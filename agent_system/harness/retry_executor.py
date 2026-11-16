# agent_system/harness/retry_executor.py
# 通用同步重试执行器：只包裹“单次可重试调用”，不包裹整个 Agent 循环。
# 与 LoopController 的边界：Loop 限制“多少轮”，Executor 限制“单次失败后重试几次”。

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from agent_system.errors.exceptions import AgentError
from agent_system.harness.retry_policy import RetryPolicy
from agent_system.harness.retry_targets import is_retryable_llm, is_retryable_tool
from agent_system.obs.events import log_event

T = TypeVar("T")


def compute_backoff_ms(
    attempt: int,
    policy: RetryPolicy,
    *,
    rand: Callable[[], float] = random.random,
) -> int:
    """计算第 attempt 次重试前的退避时间（毫秒）。

    参数:
        attempt: 从 1 开始（1 表示第一次失败后、即将第一次重试）。
        policy: 退避参数。
        rand: 随机源，返回 [0,1)；测试可注入固定值。

    返回:
        非负整数毫秒。

    异常:
        ValueError: attempt <= 0。

    说明:
        使用指数退避而非固定间隔：上游拥塞常为短时抖动，指数退避能显著降低
        持续放大故障的概率；叠加抖动可避免多实例同时重试形成脉冲式负载。
        封顶后再乘抖动可能略超 max_backoff_ms，但不超过
        max_backoff_ms * (1 + jitter_ratio)，属于可接受范围。
    """
    if attempt <= 0:
        raise ValueError("attempt 必须从 1 开始")
    base = policy.initial_backoff_ms * (policy.backoff_multiplier ** (attempt - 1))
    capped = min(base, policy.max_backoff_ms)
    # 抖动因子落在 [1-jitter, 1+jitter]
    factor = 1.0 + policy.jitter_ratio * (2 * rand() - 1)
    return max(0, int(round(capped * factor)))


class RetryExecutor:
    """同步重试执行器。

    生命周期:
        每次调用创建一个实例（轻量），不持有跨请求状态。
    与 LoopController 的叠加:
        最坏总耗时约为 max_attempts * max_steps * 单次耗时；
        生产环境必须同时用 LOOP_TIMEOUT_SECONDS 收敛上界。
    """

    def __init__(self, policy: RetryPolicy, *, target: str):
        """初始化执行器。

        参数:
            policy: Retry 策略。
            target: "llm" 或 "tool:<name>"，仅用于事件字段与白名单判断。
        """
        self.policy = policy
        self.target = target
        self.policy_id = policy.policy_id()

    def _eligible(self) -> bool:
        """判断当前 target 是否在白名单内且策略开启。"""
        if self.target == "llm":
            return is_retryable_llm(self.policy)
        if self.target.startswith("tool:"):
            return is_retryable_tool(self.target.split(":", 1)[1], self.policy)
        # 未知 target 一律不重试，避免误把写操作纳入
        return False

    @staticmethod
    def _error_code(exc: Exception) -> str:
        """提取可安全记录的错误码（AgentError.code 或异常类名）。"""
        if isinstance(exc, AgentError):
            return exc.code
        return type(exc).__name__

    def run(
        self,
        fn: Callable[[], T],
        *,
        is_retryable: Callable[[BaseException], bool] | None = None,
    ) -> T:
        """执行 fn，必要时做指数退避重试。

        参数:
            fn: 无参可调用对象。
            is_retryable: 可选判定函数；缺省用 AgentError.retryable，其他异常不重试。

        返回:
            fn 的成功返回值。

        异常:
            原样抛出最后一次失败异常（保留 __cause__ 链）。
        """
        # 总开关关闭或 target 不在白名单：直接调用一次，不产生任何 retry 事件
        if not self._eligible():
            return fn()

        started = time.monotonic()
        last_exc: Exception | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - 需要按策略决定是否重试
                last_exc = exc
                if is_retryable is not None:
                    retryable = is_retryable(exc)
                else:
                    # 缺省仅信任领域异常的 retryable 标记，其他异常不猜测
                    retryable = isinstance(exc, AgentError) and exc.retryable

                if not retryable:
                    raise

                if attempt >= self.policy.max_attempts:
                    # 已用完所有尝试：记录耗尽事件后原样抛出
                    log_event(
                        "retry_exhausted",
                        target=self.target,
                        attempts=attempt,
                        last_error_code=self._error_code(exc),
                        policy_id=self.policy_id,
                        total_elapsed_ms=round((time.monotonic() - started) * 1000, 2),
                    )
                    raise

                backoff_ms = compute_backoff_ms(attempt, self.policy)
                log_event(
                    "retry_attempt",
                    target=self.target,
                    attempt=attempt,
                    max_attempts=self.policy.max_attempts,
                    error_code=self._error_code(exc),
                    backoff_ms=backoff_ms,
                    policy_id=self.policy_id,
                )
                time.sleep(backoff_ms / 1000)

        # 理论不可达：循环最后一次失败已 raise；兜底保留异常链
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("retry executor 未执行任何尝试")

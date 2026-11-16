# agent_system/harness/loop_controller.py
# Loop Controller：把工具循环的轮次、工具调用次数、超时与外部中断检查收敛为可配置策略。
# 只负责“是否还能继续下一轮”，不执行 LLM/工具，也不吞业务异常。

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError
from agent_system.obs.events import log_event

# 与重构前 `for _ in range(5)` 对齐的默认轮次；用于兼容“默认配置静默返回”的旧行为
LEGACY_MAX_STEPS = 5

_ENV_MAX_STEPS = "LOOP_MAX_STEPS"
_ENV_MAX_TOOL_CALLS = "LOOP_MAX_TOOL_CALLS"
_ENV_TIMEOUT_SECONDS = "LOOP_TIMEOUT_SECONDS"
_ENV_ENABLE_INTERRUPT = "LOOP_ENABLE_INTERRUPT_CHECK"


class LoopTerminationReason(str, Enum):
    """循环终止原因的枚举（写入 loop_terminated 事件与 LoopState）。"""

    COMPLETED = "completed"            # 模型返回无 tool_calls 的最终回复
    MAX_STEPS = "max_steps"            # 达到最大 LLM 调用轮次
    MAX_TOOL_CALLS = "max_tool_calls"  # 达到最大工具调用次数
    TIMEOUT = "timeout"                # 超过 timeout_seconds
    INTERRUPTED = "interrupted"        # 外部中断探针返回 True
    ERROR = "error"                    # 循环内抛出未捕获异常


@dataclass(frozen=True)
class LoopPolicy:
    """循环策略参数：默认值与重构前硬编码语义等价（max_steps=5）。"""

    max_steps: int = LEGACY_MAX_STEPS
    max_tool_calls: int = 20
    timeout_seconds: float = 120.0
    enable_interrupt_check: bool = False

    def validate(self) -> None:
        """校验策略参数。

        异常:
            ValueError: 轮次/工具次数/超时非法，或工具次数小于轮次上限。

        说明:
            `max_tool_calls >= max_steps` 是约束而非巧合：默认配置下如果工具次数
            更小，会先触发 max_tool_calls，使 max_steps 永远不可达，难以诊断。
        """
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool) or self.max_steps <= 0:
            raise ValueError(f"max_steps 必须为正整数，当前值: {self.max_steps!r}")
        if (
            not isinstance(self.max_tool_calls, int)
            or isinstance(self.max_tool_calls, bool)
            or self.max_tool_calls < self.max_steps
        ):
            raise ValueError(
                "max_tool_calls 必须为不小于 max_steps 的整数"
                f"（当前 {self.max_tool_calls!r} < {self.max_steps}）"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正数，当前值: {self.timeout_seconds!r}")

    def policy_id(self) -> str:
        """返回策略的稳定短哈希（8 位十六进制），便于阶段六联合分析。"""
        raw = "|".join(
            [
                str(self.max_steps),
                str(self.max_tool_calls),
                str(self.timeout_seconds),
                str(self.enable_interrupt_check),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def from_env(cls) -> tuple["LoopPolicy", list[str]]:
        """从 LOOP_* 环境变量构造策略。

        返回:
            (policy, invalid_env)：invalid_env 只记录变量名，不记录值。

        说明:
            非法值/非正数回退默认值，保证配置写错也能启动。
        """
        invalid: list[str] = []
        policy = LoopPolicy(
            max_steps=_read_int(_ENV_MAX_STEPS, LEGACY_MAX_STEPS, invalid),
            max_tool_calls=_read_int(_ENV_MAX_TOOL_CALLS, 20, invalid),
            timeout_seconds=_read_float(_ENV_TIMEOUT_SECONDS, 120.0, invalid),
            enable_interrupt_check=_read_bool(_ENV_ENABLE_INTERRUPT, False, invalid),
        )
        return policy, invalid


@dataclass
class LoopState:
    """一次工具循环的运行期状态（可变，仅由 Controller 与调用方更新）。"""

    policy: LoopPolicy
    steps: int = 0
    tool_calls: int = 0
    started_at: float = 0.0
    terminated_reason: LoopTerminationReason | None = None
    elapsed_ms: float = 0.0


def _read_int(name: str, default: int, invalid: list[str]) -> int:
    """读取整数环境变量；缺失用默认值，非法/非正数记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        invalid.append(name)
        return default
    if value <= 0:
        invalid.append(name)
        return default
    return value


def _read_float(name: str, default: float, invalid: list[str]) -> float:
    """读取浮点环境变量；缺失用默认值，非法/非正数记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        invalid.append(name)
        return default
    if value <= 0:
        invalid.append(name)
        return default
    return value


def _read_bool(name: str, default: bool, invalid: list[str]) -> bool:
    """读取布尔环境变量；非法值记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    invalid.append(name)
    return default


class LoopController:
    """按策略判断循环能否继续，并记录 loop_* 结构化事件。

    生命周期:
        默认由 `get_loop_controller()` 单例持有；测试可构造独立实例。
        每个请求调用 `begin()` 生成独立 LoopState，实例本身无请求级状态。
    """

    def __init__(self, policy: LoopPolicy):
        """保存并校验策略。

        参数:
            policy: 循环策略。

        异常:
            ValueError: policy.validate() 不通过。
        """
        policy.validate()
        self.policy = policy
        self.policy_id = policy.policy_id()

    def begin(self) -> LoopState:
        """开始一次循环并记录 loop_started。

        返回:
            新的 LoopState（started_at 使用 time.monotonic）。
        """
        state = LoopState(policy=self.policy, started_at=time.monotonic())
        log_event(
            "loop_started",
            max_steps=self.policy.max_steps,
            max_tool_calls=self.policy.max_tool_calls,
            timeout_seconds=self.policy.timeout_seconds,
            policy_id=self.policy_id,
        )
        return state

    def check(
        self,
        state: LoopState,
        *,
        interrupt_probe: Callable[[], bool] | None = None,
    ) -> None:
        """检查是否允许发起下一轮 LLM 调用。

        参数:
            state: 当前循环状态。
            interrupt_probe: 可选中断探针；仅当策略开启中断检查时调用。

        异常:
            AgentError: code 为 loop_exhausted / loop_timeout / loop_interrupted。

        说明:
            调用方必须在“发起下一次 LLM 调用之前”调用本方法，因此
            `max_steps=N` 精确等价于“最多 N 次 LLM 调用”，与原 5 轮硬编码一致。
        """
        # 1) 轮次上限：steps 只在 LLM 调用返回后递增，失败调用不会误占配额
        if state.steps >= self.policy.max_steps:
            raise AgentError(
                ErrorCode.LOOP_EXHAUSTED,
                details={
                    "reason": "max_steps",
                    "steps": state.steps,
                    "tool_calls": state.tool_calls,
                },
            )

        # 2) 工具调用次数上限：允许最后一次工具调用完成后再拦截
        if state.tool_calls >= self.policy.max_tool_calls:
            raise AgentError(
                ErrorCode.LOOP_EXHAUSTED,
                details={
                    "reason": "max_tool_calls",
                    "steps": state.steps,
                    "tool_calls": state.tool_calls,
                },
            )

        # 3) 超时：轮询式检查，不引入 signal/Timer，跨平台且可测试
        elapsed = time.monotonic() - state.started_at
        if elapsed > self.policy.timeout_seconds:
            raise AgentError(
                ErrorCode.LOOP_TIMEOUT,
                details={"steps": state.steps, "tool_calls": state.tool_calls},
            )

        # 4) 外部中断：默认关闭，避免影响既有行为；阶段四可注入请求取消探针
        if self.policy.enable_interrupt_check and interrupt_probe is not None:
            if interrupt_probe():
                raise AgentError(
                    ErrorCode.LOOP_INTERRUPTED,
                    details={"steps": state.steps, "tool_calls": state.tool_calls},
                )

    def record_tool_call(self, state: LoopState, *, tool_name: str) -> None:
        """记录一次工具调用计数（不在此处检查上限）。

        参数:
            state: 当前循环状态。
            tool_name: 工具名；当前只用于扩展/诊断，不进入 state。
        """
        state.tool_calls += 1

    def log_step(self, state: LoopState) -> None:
        """记录一轮工具调用完成后的 loop_step 事件。"""
        log_event(
            "loop_step",
            step_index=state.steps,
            elapsed_ms=round((time.monotonic() - state.started_at) * 1000, 2),
            tool_calls_so_far=state.tool_calls,
        )

    def finish(self, state: LoopState, reason: LoopTerminationReason) -> LoopState:
        """写入终止原因与耗时，并记录一次 loop_terminated。

        参数:
            state: 当前循环状态。
            reason: 终止原因。

        返回:
            更新后的 state。

        说明:
            幂等：重复调用不会重复记录日志，便于调用方在 except/finally 中都安全调用。
        """
        if state.terminated_reason is not None:
            return state
        state.terminated_reason = reason
        state.elapsed_ms = round((time.monotonic() - state.started_at) * 1000, 2)
        log_event(
            "loop_terminated",
            reason=reason.value,
            steps=state.steps,
            tool_calls=state.tool_calls,
            elapsed_ms=state.elapsed_ms,
            policy_id=self.policy_id,
        )
        return state


def termination_reason_for(exc: Exception) -> LoopTerminationReason:
    """把循环中抛出的异常映射为 LoopTerminationReason。

    参数:
        exc: 循环内抛出的异常。

    返回:
        对应的终止原因；非循环类异常一律归为 ERROR。
    """
    if isinstance(exc, AgentError):
        if exc.code == ErrorCode.LOOP_EXHAUSTED.value:
            return (
                LoopTerminationReason.MAX_TOOL_CALLS
                if exc.details.get("reason") == "max_tool_calls"
                else LoopTerminationReason.MAX_STEPS
            )
        if exc.code == ErrorCode.LOOP_TIMEOUT.value:
            return LoopTerminationReason.TIMEOUT
        if exc.code == ErrorCode.LOOP_INTERRUPTED.value:
            return LoopTerminationReason.INTERRUPTED
    return LoopTerminationReason.ERROR


_controller: LoopController | None = None


def get_loop_controller() -> LoopController:
    """返回全局 LoopController 单例；首次调用时加载策略并记录 loop_policy_loaded。"""
    global _controller
    if _controller is None:
        policy, invalid_env = LoopPolicy.from_env()
        _controller = LoopController(policy)
        log_event(
            "loop_policy_loaded",
            policy_id=_controller.policy_id,
            max_steps=policy.max_steps,
            max_tool_calls=policy.max_tool_calls,
            timeout_seconds=policy.timeout_seconds,
            enable_interrupt_check=policy.enable_interrupt_check,
            invalid_env=invalid_env,
        )
    return _controller


def reset_loop_controller() -> None:
    """重置全局 Controller（仅供测试隔离）。

    说明:
        生产代码不得调用：重置后下次使用会重新读取环境变量，可能造成策略突变。
    """
    global _controller
    _controller = None

# agent_system/errors/fallback.py
# 兜底接入点：只定义协议与默认空实现，不包含任何重试/退避/降级逻辑。
# 阶段三可注册带 Retry 的实现替换默认 hook，四个调用点保持不变。
#
# 为什么要这层抽象：错误处理有四个入口（AgentError / 参数校验 / HTTPException /
# 未捕获异常），若把「要不要补救」分别写进四个处理器，代码会散落成四份；
# 抽成一个函数后，策略只有一处可替换。

from typing import Optional, Protocol

from agent_system.errors.exceptions import AgentError
from agent_system.obs.events import error_raised, log_event


class FallbackHook(Protocol):
    """兜底钩子协议：返回替换后的错误，或原样返回。

    为什么是 async：阶段三的重试需要 await 一次网络调用；现在定成同步函数，
    将来改签名就会破坏所有调用点。
    为什么返回 Optional[AgentError] 而不是响应对象：钩子只能改写错误的分类，
    不能把失败伪装成成功，否则会出现「500 悄悄变 200」的静默失败。
    """

    async def handle(self, error: AgentError, context: dict) -> Optional[AgentError]:
        """处理一个领域异常。

        参数：
            error: 已分类的领域异常。
            context: 定位信息，含 request_id / path / session_id / tool
                     （后两者可能为 None）。
        返回：
            一个 AgentError 表示替换原错误；返回 None 表示不替换，沿用原错误。
        """
        ...


class NoOpFallbackHook:
    """默认实现：记录 error_raised 后原样返回，不重试、不降级。

    这里的「什么都不做」是刻意的：本阶段的目标是先让错误契约稳定，
    默认实现一旦开始改判定或降级，之前的错误码语义与测试都会失效。
    """

    async def handle(self, error: AgentError, context: dict) -> Optional[AgentError]:
        """记录一条 error_raised 事件，然后把原错误原样返回。"""
        cause = error.cause
        error_raised(
            error.code,
            error.retryable,
            # 只记录异常类型，不记录异常文本与堆栈
            type(cause).__name__ if cause is not None else None,
            request_id=context.get("request_id"),
            path=context.get("path"),
            tool=context.get("tool"),
            session_id=context.get("session_id"),
        )
        return error


class RetryObservingHook:
    """Retry 启用时的观察实现：只记录事件，不改变异常流向。

    与 NoOpFallbackHook 的关系：
        - 默认关闭 Retry 时仍用 NoOpFallbackHook；
        - 开启 Retry 后替换为本类，额外记录 `retry_exhausted_observed`，
          但依旧原样返回 error，绝不把失败伪装成成功。

    为什么叫 observed 而不是 retry_exhausted：
        executor 内部已经在真正耗尽时记录 `retry_exhausted`；
        本钩子位于异常出口，无法确认异常一定来自重试耗尽（也可能首次即不可重试），
        因此用独立事件名避免与执行器重复计数。
    """

    async def handle(self, error: AgentError, context: dict) -> Optional[AgentError]:
        """记录 error_raised 与（可重试错误的）retry_exhausted_observed。"""
        cause = error.cause
        error_raised(
            error.code,
            error.retryable,
            type(cause).__name__ if cause is not None else None,
            request_id=context.get("request_id"),
            path=context.get("path"),
            tool=context.get("tool"),
            session_id=context.get("session_id"),
        )

        # 延迟导入，避免 errors 包在 import 期依赖 harness
        from agent_system.harness.retry_policy import get_retry_policy

        policy = get_retry_policy()
        if policy.enabled and error.retryable:
            log_event(
                "retry_exhausted_observed",
                code=error.code,
                retryable=error.retryable,
                request_id=context.get("request_id"),
                path=context.get("path"),
            )
        return error


# 当前生效的钩子；模块级单例，便于在启动时替换
_hook: FallbackHook = NoOpFallbackHook()


def register_hook(hook: FallbackHook) -> None:
    """替换全局兜底钩子；阶段三在这里注入带重试的实现，调用点无需改动。"""
    global _hook
    _hook = hook


def get_hook() -> FallbackHook:
    """取得当前兜底钩子；异常处理器每次调用时实时获取，保证替换立即生效。"""
    return _hook

# agent_system/harness/retry_targets.py
# 重试白名单：只有“幂等读工具”与 LLM 调用允许重试。

from __future__ import annotations

from agent_system.harness.retry_policy import RetryPolicy, get_retry_policy

# 幂等读工具：重复调用不会修改数据，失败重试安全
IDEMPOTENT_READ_TOOLS: frozenset[str] = frozenset(
    {"query_product", "query_order", "query_logistics", "search_faq"}
)

# 非重试工具：写操作与确认工具
# - apply_refund / cancel_order：会修改 orders.json；若“已执行但响应丢失”，
#   重试可能造成重复退款/重复取消，服务端无法安全区分；
# - confirm_action / reject_action：两阶段状态机本身已保证幂等，
#   重试只会加剧并发压力，没有收益。
NON_RETRYABLE_TOOLS: frozenset[str] = frozenset(
    {"apply_refund", "cancel_order", "confirm_action", "reject_action"}
)


def is_retryable_tool(name: str, policy: RetryPolicy | None = None) -> bool:
    """判断指定工具是否允许重试。

    参数:
        name: 工具名。
        policy: 可选策略；缺省读取全局单例。

    返回:
        True 表示是幂等读工具且策略开启。
    """
    current = policy or get_retry_policy()
    return bool(current.enabled and current.retry_tools and name in IDEMPOTENT_READ_TOOLS)


def is_retryable_llm(policy: RetryPolicy | None = None) -> bool:
    """判断 LLM 调用是否允许重试。

    参数:
        policy: 可选策略；缺省读取全局单例。

    返回:
        True 表示策略开启且 retry_llm=True。

    说明:
        LLM 调用天然幂等（一次请求一次响应），失败重试不会产生业务副作用。
    """
    current = policy or get_retry_policy()
    return bool(current.enabled and current.retry_llm)

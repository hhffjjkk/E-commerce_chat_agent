# agent_system/harness/builtin_profiles.py
# 三个内置子 Agent：售前 / 订单与物流 / 售后。
# 本模块只定义静态 Profile 与 prompt 组合，不修改 agent.SYSTEM_PROMPT，也不接入路由。

from __future__ import annotations

from typing import Callable

from agent_system.harness.agent_profile import (
    AgentProfile,
    ProfileRegistry,
    get_profile_registry,
)


def _load_base_rules() -> str:
    """延迟读取 agent.SYSTEM_PROMPT，作为所有子 Agent 的共享基础规则。

    返回:
        当前 SYSTEM_PROMPT 原文。

    说明:
        通过 provider 而非模块级 import 取常量，避免 builtin_profiles 与 agent
        的循环导入；同时为阶段六“prompt 版本化”预留注入点。
    """
    from agent_system.agent import SYSTEM_PROMPT

    return SYSTEM_PROMPT


# Router/组合函数通过该 provider 取共享规则；测试可 monkeypatch 本模块属性
BASE_RULES_PROVIDER: Callable[[], str] = _load_base_rules


def compose_system_prompt(profile: AgentProfile) -> str:
    """组合子 Agent 的完整 system prompt。

    参数:
        profile: 子 Agent 声明。

    返回:
        共享基础规则 + 子 Agent 标题 + 专属规则。

    说明:
        顺序从稳定到易变：共享规则与标题在 Profile 固定时逐字稳定，专属规则是
        设计期常量，因此同一 Profile 的输出对 ContextAssembler 的稳定前缀哈希稳定，
        不会随请求变化；这也是不能把动态内容拼进 prompt 的原因。
    """
    base = BASE_RULES_PROVIDER()
    # 共享规则在前：保证所有子 Agent 都遵守“转人工、不臆造、写操作确认”等底线
    # 专属规则在后：越靠后越具体，便于模型理解当前角色的额外边界
    return f"{base}\n\n## 当前子 Agent：{profile.display_name}\n\n{profile.system_prompt}"


# 售前：只读商品与 FAQ，不触达订单/售后；避免售前 Agent 越权执行写操作
PRE_SALES_PROFILE = AgentProfile(
    name="pre_sales",
    display_name="售前咨询",
    description="商品信息、价格、库存、功能咨询、选购建议、常见问题",
    system_prompt=(
        "你是售前咨询助手。\n"
        "- 优先用 query_product 查询商品信息、用 search_faq 查询常见问题。\n"
        "- 不涉及订单、退款、取消等售后操作；遇到此类诉求，礼貌说明需转售后。\n"
        "- 回答简洁，给出关键参数（价格、库存、适用场景），不臆造未查询到的信息。"
    ),
    tool_names=("query_product", "search_faq"),
    allowed_write_tools=(),
    tags=("business", "pre_sales"),
)

# 订单：只读订单与物流；不持有写权限，遇到退款/取消引导到售后
ORDER_PROFILE = AgentProfile(
    name="order",
    display_name="订单与物流",
    description="订单状态、物流轨迹、收货信息查询",
    system_prompt=(
        "你是订单与物流查询助手。\n"
        "- 用 query_order 查询订单状态、收货信息；用 query_logistics 查询物流轨迹。\n"
        "- 不处理退款与取消；遇到售后诉求，礼貌说明需转售后。\n"
        '- 查询未命中时明确告知用户“未找到该订单/物流记录”，不要猜测。'
    ),
    tool_names=("query_order", "query_logistics"),
    allowed_write_tools=(),
    tags=("business", "order"),
)

# 售后：需要 query_order 做前置状态判断；写操作仍由 Guardrails 两阶段确认拦截。
# 这里只声明“允许触发哪些写工具”，不替代 Guardrails 的执行前拦截。
AFTER_SALES_PROFILE = AgentProfile(
    name="after_sales",
    display_name="售后服务",
    description="退款、取消订单、退货、售后处理",
    system_prompt=(
        "你是售后处理助手。\n"
        "- 先用 query_order 确认订单状态，再决定是否可退款/可取消。\n"
        "- 退款与取消必须走两阶段确认：先调用 apply_refund / cancel_order 拿到 needs_confirmation 与 summary，\n"
        "  向用户复述摘要并请求明确确认；用户确认后调用 confirm_action，拒绝则调用 reject_action。\n"
        "- 未经用户明确确认，不得直接执行写操作，也不得重复调用 apply_refund / cancel_order 绕过确认。\n"
        "- 执行结果如实告知用户；失败时说明原因，不承诺无法保证的结果。"
    ),
    tool_names=(
        "query_order",
        "apply_refund",
        "cancel_order",
        "confirm_action",
        "reject_action",
    ),
    allowed_write_tools=("apply_refund", "cancel_order"),
    tags=("business", "after_sales"),
)

BUILTIN_PROFILES: tuple[AgentProfile, ...] = (
    PRE_SALES_PROFILE,
    ORDER_PROFILE,
    AFTER_SALES_PROFILE,
)


def register_builtin_profiles(registry: ProfileRegistry | None = None) -> None:
    """注册三个内置 Profile（幂等）。

    参数:
        registry: 目标注册表；None 时使用全局单例。

    异常:
        ValueError: Tool Registry 未初始化、Profile 校验失败，或同名定义不同。

    说明:
        前置条件：必须先调用 `builtin_tools.register_all()`，否则 validate 会提示
        “Tool Registry 为空”。幂等策略与 builtin_tools 一致：同 spec 跳过，
        不同 spec 抛错，防止运行期被意外覆写。
    """
    target = registry if registry is not None else get_profile_registry()
    for profile in BUILTIN_PROFILES:
        existing = target.get(profile.name)
        if existing is not None:
            if existing == profile:
                continue
            raise ValueError(
                f"builtin Profile 定义被并发修改: {profile.name}（请检查是否重复注册）"
            )
        target.register(profile)

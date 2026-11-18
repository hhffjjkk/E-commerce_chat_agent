# agent_system/harness/selection.py
# 会话级 Profile 选择：pending 粘滞 -> Router -> 默认 Profile。
# 放在 harness 而不是 app.py：demo.py 也需要同一套三级决策，
# 且 pending 粘滞属于会话层领域规则，不属于通用 Router。

from __future__ import annotations

import time

from agent_system.harness.agent_profile import AgentProfile, get_profile_registry
from agent_system.harness.guardrails import get_guardrails
from agent_system.harness.router import RouteDecision, get_router
from agent_system.obs.events import log_event


def _default_profile() -> AgentProfile:
    """返回兜底 Profile：优先路由策略配置，其次注册表第一个。

    返回:
        可用的 AgentProfile。

    异常:
        RuntimeError: ProfileRegistry 为空（启动时未注册内置 Profile）。
    """
    registry = get_profile_registry()
    router = get_router()
    profile = registry.get(router.policy.default_profile)
    if profile is not None:
        return profile
    profiles = registry.list()
    if not profiles:
        raise RuntimeError("ProfileRegistry 为空，请先注册内置 Profile")
    return profiles[0]


def select_profile(
    session_id: str,
    message: str,
) -> tuple[AgentProfile, RouteDecision | None, str]:
    """选择本次请求使用的子 Agent Profile。

    决策顺序（前一级命中即返回）：
      1. pending 粘滞：会话存在未过期 pending action 时强制 after_sales
         （reason=pending_override，decision=None，不伪造 Router 决策）；
      2. Router：走 get_router().route(message)，reason=router；
      3. 防御性回退：Router 结果不在注册表 / Router 抛异常 / 注册表异常时，
         使用默认 Profile（reason=profile_missing / router_exception）。

    参数:
        session_id: 会话 ID。
        message: 用户消息。

    返回:
        (profile, decision, reason)。decision 为 None 仅表示“非 Router 决策”。

    说明:
        pending 粘滞只读 Guardrails.list_pending，不写 pending；
        评审/确认状态的其他动作不粘滞（只有 pending 需要用户下一步确认）。
    """
    started = time.monotonic()
    registry = get_profile_registry()

    # 1) pending 粘滞：会话内有待确认动作时，后续“确认/拒绝”必须回到售后 Agent
    try:
        pendings = get_guardrails().list_pending(session_id)
    except Exception:  # noqa: BLE001 - 读取 pending 失败不应阻塞路由
        pendings = []
    if pendings:
        after_sales = registry.get("after_sales")
        if after_sales is not None:
            _log_selected(after_sales, None, "pending_override", 1.0, started)
            return after_sales, None, "pending_override"

    # 2) Router 决策；Router 内部已有三级兜底，这里再防一层异常
    try:
        decision = get_router().route(message)
    except Exception as exc:  # noqa: BLE001 - 路由失败必须回退而不是中断请求
        log_event(
            "route_selection_failed",
            reason="router_exception",
            error_type=type(exc).__name__,
        )
        profile = _default_profile()
        _log_selected(profile, None, "router_exception", 0.0, started)
        return profile, None, "router_exception"

    # 3) 防御性校验：RouteDecision.agent 理论上一定在注册表内
    profile = registry.get(decision.agent)
    if profile is None:
        profile = _default_profile()
        _log_selected(profile, decision, "profile_missing", 0.0, started)
        return profile, decision, "profile_missing"

    _log_selected(profile, decision, "router", decision.confidence, started)
    return profile, decision, "router"


def _log_selected(
    profile: AgentProfile,
    decision: RouteDecision | None,
    reason: str,
    confidence: float,
    started: float,
) -> None:
    """记录 route_selected 事件（不含用户消息原文）。"""
    log_event(
        "route_selected",
        agent=profile.name,
        display_name=profile.display_name,
        strategy=(decision.strategy.value if decision is not None else "pending_override"),
        reason=reason,
        confidence=round(confidence, 4),
        latency_ms=round((time.monotonic() - started) * 1000, 2),
    )

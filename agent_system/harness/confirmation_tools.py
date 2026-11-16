# agent_system/harness/confirmation_tools.py
# 写操作确认/拒绝工具：供 LLM 在用户明确确认或反悔后调用。
# 真正的状态机与执行由 Guardrails 负责，这里只做会话校验与委托。

from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError
from agent_system.harness.guardrails import get_guardrails
from agent_system.obs.request_id import get_session_id
from agent_system.tools import _tool_guard


def _session_or_error(tool: str) -> str:
    """取当前会话；缺失时抛 confirmation_required（不允许无会话确认）。"""
    session_id = get_session_id()
    if not session_id:
        raise AgentError(
            ErrorCode.CONFIRMATION_REQUIRED,
            details={"tool": tool},
        )
    return session_id


@_tool_guard("confirm_action")
def confirm_action(action_id: str) -> dict:
    """确认并执行之前被拦截的写操作。

    参数:
        action_id: intercept 阶段返回的待确认 ID。

    返回:
        {"success": True, "action_id", "result"}；重复确认返回缓存结果。

    异常:
        AgentError: 不存在/过期 410、跨会话或并发冲突 409、执行失败原错误码。
    """
    session_id = _session_or_error("confirm_action")
    return get_guardrails().confirm(action_id, session_id)


@_tool_guard("reject_action")
def reject_action(action_id: str) -> dict:
    """拒绝之前被拦截的写操作（幂等）。

    参数:
        action_id: intercept 阶段返回的待确认 ID。

    返回:
        {"success": True, "action_id", "status"}。

    异常:
        AgentError: 不存在/过期 410、跨会话 409。
    """
    session_id = _session_or_error("reject_action")
    return get_guardrails().reject(action_id, session_id)

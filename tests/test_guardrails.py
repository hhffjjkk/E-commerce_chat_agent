"""Guardrails 单测：拦截、确认、拒绝、过期、幂等、开关与日志。"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system import tools  # noqa: E402
from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.harness import guardrails as guardrails_module  # noqa: E402
from agent_system.harness.guardrails import Guardrails  # noqa: E402
from agent_system.harness.pending_store import InMemoryPendingStore  # noqa: E402


@pytest.fixture
def events(monkeypatch):
    """收集 confirmation_* 日志事件。"""
    captured = []

    def fake_log_event(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(guardrails_module, "log_event", fake_log_event)
    return captured


@pytest.fixture
def impl_calls(monkeypatch):
    """替换写操作实现，记录调用次数与参数（不触碰真实数据）。"""
    calls = []

    def fake_apply(order_id, reason):
        calls.append(("apply_refund", order_id, reason))
        return {"found": True, "success": True, "message": f"退款已提交: {order_id}"}

    def fake_cancel(order_id):
        calls.append(("cancel_order", order_id))
        return {"found": True, "success": True, "message": f"订单已取消: {order_id}"}

    monkeypatch.setattr(tools, "_apply_refund_impl", fake_apply)
    monkeypatch.setattr(tools, "_cancel_order_impl", fake_cancel)
    return calls


def _guardrails(**overrides) -> Guardrails:
    """构造使用独立 Store 的 Guardrails。"""
    return Guardrails(store=InMemoryPendingStore(), **overrides)


ARGS = {"order_id": "20261103001", "reason": "不想要了"}


def test_intercept_creates_pending_without_executing(events, impl_calls):
    """首次调用只创建 pending，不执行实现。"""
    guardrails = _guardrails()
    payload = guardrails.intercept_write("apply_refund", ARGS, "s1")

    assert payload["needs_confirmation"] is True
    assert payload["action_id"]
    assert payload["summary"] == "为订单 20261103001 申请退款，原因：不想要了"
    assert payload["expires_in"] > 0
    assert impl_calls == []

    requested = [e for e in events if e["event"] == "confirmation_requested"]
    assert len(requested) == 1
    assert requested[0]["tool"] == "apply_refund"
    assert requested[0]["session_id"] == "s1"


def test_intercept_is_idempotent_for_same_args(events, impl_calls):
    """同会话、同工具、同参数重复拦截返回同一 action_id。"""
    guardrails = _guardrails()
    first = guardrails.intercept_write("apply_refund", ARGS, "s1")
    second = guardrails.intercept_write("apply_refund", dict(ARGS), "s1")

    assert first["action_id"] == second["action_id"]
    assert len(guardrails.list_pending("s1")) == 1


def test_intercept_different_args_create_different_actions(events, impl_calls):
    """参数不同则指纹不同，创建新的 action。"""
    guardrails = _guardrails()
    first = guardrails.intercept_write("apply_refund", ARGS, "s1")
    second = guardrails.intercept_write(
        "apply_refund", {**ARGS, "reason": "尺码不合适"}, "s1"
    )
    assert first["action_id"] != second["action_id"]


def test_confirm_executes_and_is_idempotent(events, impl_calls):
    """pending -> executed；重复 confirm 返回缓存结果且只执行一次。"""
    guardrails = _guardrails()
    action_id = guardrails.intercept_write("apply_refund", ARGS, "s1")["action_id"]

    result = guardrails.confirm(action_id, "s1")
    assert result["success"] is True
    assert result["result"]["message"] == "退款已提交: 20261103001"
    assert impl_calls == [("apply_refund", "20261103001", "不想要了")]

    again = guardrails.confirm(action_id, "s1")
    assert again == result
    assert len(impl_calls) == 1
    assert len([e for e in events if e["event"] == "confirmation_granted"]) == 1


def test_confirm_rejects_cross_session(events, impl_calls):
    """跨会话确认返回 confirmation_mismatch。"""
    guardrails = _guardrails()
    action_id = guardrails.intercept_write("apply_refund", ARGS, "s1")["action_id"]

    with pytest.raises(AgentError) as exc_info:
        guardrails.confirm(action_id, "s2")
    assert exc_info.value.code == "confirmation_mismatch"
    assert exc_info.value.http_status == 409


def test_confirm_concurrent_confirmed_state_is_mismatch(events, impl_calls):
    """记录处于 confirmed（执行中）时再次 confirm 抛 409，避免双写。"""
    guardrails = _guardrails()
    action_id = guardrails.intercept_write("apply_refund", ARGS, "s1")["action_id"]
    guardrails.store.mark(action_id, "confirmed")

    with pytest.raises(AgentError) as exc_info:
        guardrails.confirm(action_id, "s1")
    assert exc_info.value.code == "confirmation_mismatch"


def test_confirm_expired(events, impl_calls):
    """超过 TTL 后 confirm 返回 confirmation_expired（410）。"""
    guardrails = _guardrails()
    action_id = guardrails.intercept_write("apply_refund", ARGS, "s1")["action_id"]
    guardrails.store.get(action_id).expires_at = time.monotonic() - 1

    with pytest.raises(AgentError) as exc_info:
        guardrails.confirm(action_id, "s1")
    assert exc_info.value.code == "confirmation_expired"
    assert exc_info.value.http_status == 410


def test_confirm_failure_marks_rejected(events, monkeypatch):
    """执行失败时记录转 rejected，并原样抛出业务错误。"""
    guardrails = _guardrails()

    def fail(order_id, reason):
        raise AgentError("tool_failed")

    monkeypatch.setattr(tools, "_apply_refund_impl", fail)
    action_id = guardrails.intercept_write("apply_refund", ARGS, "s1")["action_id"]

    with pytest.raises(AgentError) as exc_info:
        guardrails.confirm(action_id, "s1")
    assert exc_info.value.code == "tool_failed"
    assert guardrails.store.get(action_id).status == "rejected"
    failed = [e for e in events if e["event"] == "confirmation_failed"]
    assert len(failed) == 1
    assert failed[0]["error_code"] == "tool_failed"

    # 再次确认同一记录不应重新执行
    with pytest.raises(AgentError) as again:
        guardrails.confirm(action_id, "s1")
    assert again.value.code == "confirmation_expired"


def test_reject_and_idempotency(events, impl_calls):
    """pending -> rejected，重复 reject 幂等返回。"""
    guardrails = _guardrails()
    action_id = guardrails.intercept_write("cancel_order", {"order_id": "20261103001"}, "s1")["action_id"]

    first = guardrails.reject(action_id, "s1")
    second = guardrails.reject(action_id, "s1")
    assert first["status"] == "rejected"
    assert second["status"] == "rejected"
    assert len([e for e in events if e["event"] == "confirmation_rejected"]) == 1
    assert impl_calls == []


def test_disabled_executes_directly(events, impl_calls):
    """CONFIRMATION_ENABLED=false 风格的实例直接执行。"""
    guardrails = _guardrails(enabled=False)
    result = guardrails.intercept_write("apply_refund", ARGS, "s1")

    assert "needs_confirmation" not in result
    assert result["success"] is True
    assert impl_calls == [("apply_refund", "20261103001", "不想要了")]


def test_max_pending_per_session_expires_oldest(events, impl_calls):
    """单会话 pending 超过上限时最旧的被作废。"""
    guardrails = _guardrails(max_pending_per_session=1)
    first = guardrails.intercept_write("apply_refund", ARGS, "s1")
    second = guardrails.intercept_write(
        "apply_refund", {**ARGS, "reason": "换一个原因"}, "s1"
    )

    assert first["action_id"] != second["action_id"]
    assert guardrails.store.get(first["action_id"]).status == "expired"
    assert [p["action_id"] for p in guardrails.list_pending("s1")] == [
        second["action_id"]
    ]


def test_summary_contains_no_full_args_dump(events, impl_calls):
    """摘要只含安全字段，不包含参数 JSON 原文。"""
    guardrails = _guardrails()
    payload = guardrails.intercept_write("apply_refund", ARGS, "s1")
    assert json.dumps(ARGS, ensure_ascii=False) not in payload["summary"]

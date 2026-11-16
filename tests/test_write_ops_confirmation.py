"""写操作两阶段确认集成测试：fake LLM、会话隔离、过期与 NDJSON 不变。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system import tools  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.harness import confirmation_tools  # noqa: E402
from agent_system.harness.guardrails import Guardrails, reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import (  # noqa: E402
    InMemoryPendingStore,
    get_pending_store,
    reset_pending_store,
)
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402
from agent_system.obs.request_id import reset_session_id, set_session_id  # noqa: E402


class ListHandler(logging.Handler):
    """收集结构化日志。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """临时挂载 JSON handler。"""
    logger = get_logger()
    handler = ListHandler()
    handler.setFormatter(JSONFormatter("test-service"))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture(autouse=True)
def fresh_stores(tmp_path):
    """重置 Guardrails / pending store，并使用临时 SQLite 会话库。"""
    from agent_system.memory import SessionStore

    reset_guardrails()
    reset_pending_store()
    original_store = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        reset_guardrails()
        reset_pending_store()


def _events(handler):
    """解析日志记录。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _tool_call(name, arguments):
    """构造 tool_call。"""
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(tool_calls=None, content=None):
    """构造 create 返回。"""
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ConfirmCompletions:
    """模拟两轮工具调用：apply_refund -> confirm_action -> 最终文本。"""

    def __init__(self):
        self.calls = []
        self.action_id = None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        if index == 1:
            return _response(
                tool_calls=[
                    _tool_call(
                        "apply_refund",
                        {"order_id": "20261103001", "reason": "不想要了"},
                    )
                ]
            )
        if index == 2:
            # 从上一轮 tool 消息中取出服务端生成的 action_id
            for message in reversed(kwargs["messages"]):
                if isinstance(message, dict) and message.get("role") == "tool":
                    self.action_id = json.loads(message["content"])["action_id"]
                    break
            return _response(
                tool_calls=[_tool_call("confirm_action", {"action_id": self.action_id})]
            )
        if index == 3:
            return _response(content="已完成")
        return _response(content="退款已提交，请留意审核结果。")


def _bot(completions):
    """构造绕过 __init__ 的 LLMClient。"""
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    bot.model = "fake-model"
    bot._interrupt_probe = None
    bot._last_loop = None
    return bot


def _post(path: str, body: dict, request_id: str):
    """通过 ASGITransport 发起 POST。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def test_two_phase_write_via_llm(monkeypatch, captured_logs):
    """fake LLM 先退款、再确认：最终回复成功，pending 状态为 executed。"""
    applied = []

    class FakeSource:
        def apply_refund(self, order_id, reason):
            applied.append((order_id, reason))
            return {"found": True, "success": True, "message": "退款已提交"}

    monkeypatch.setattr(tools, "_source", FakeSource())
    completions = ConfirmCompletions()
    monkeypatch.setattr(app_module, "bot", _bot(completions))

    response = _post(
        "/chat",
        {"session_id": "sess-A", "message": "帮我把订单 20261103001 退款"},
        "confirm-e2e-0001",
    )

    assert response.status_code == 200
    assert "退款已提交" in response.json()["reply"]
    assert applied == [("20261103001", "不想要了")]
    assert completions.action_id
    assert get_pending_store().get(completions.action_id).status == "executed"

    events = [e for e in _events(captured_logs) if e["event"].startswith("confirmation_")]
    names = [e["event"] for e in events]
    assert names.count("confirmation_requested") == 1
    assert names.count("confirmation_granted") == 1


def test_cross_session_confirm_is_rejected(monkeypatch):
    """跨会话确认返回 409 confirmation_mismatch。"""
    guardrails = Guardrails(ttl_seconds=300, store=InMemoryPendingStore())
    payload = guardrails.intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "sess-A"
    )
    monkeypatch.setattr(confirmation_tools, "get_guardrails", lambda: guardrails)

    token = set_session_id("sess-B")
    try:
        with pytest.raises(AgentError) as exc_info:
            confirmation_tools.confirm_action(payload["action_id"])
    finally:
        reset_session_id(token)

    assert exc_info.value.code == "confirmation_mismatch"
    assert exc_info.value.http_status == 409


def test_expired_confirmation(monkeypatch):
    """TTL=0 时确认立即过期，返回 410 confirmation_expired。"""
    guardrails = Guardrails(ttl_seconds=0, store=InMemoryPendingStore())
    payload = guardrails.intercept_write(
        "apply_refund", {"order_id": "20261103001", "reason": "test"}, "sess-A"
    )
    monkeypatch.setattr(confirmation_tools, "get_guardrails", lambda: guardrails)

    token = set_session_id("sess-A")
    try:
        with pytest.raises(AgentError) as exc_info:
            confirmation_tools.confirm_action(payload["action_id"])
    finally:
        reset_session_id(token)

    assert exc_info.value.code == "confirmation_expired"
    assert exc_info.value.http_status == 410


def test_direct_write_without_session_is_rejected():
    """无会话上下文直接调用写工具时拒绝执行，而不是静默成功。"""
    # 直接用当前测试上下文（未 set_session_id）调用
    with pytest.raises(AgentError) as exc_info:
        tools.apply_refund("20261103001", "test")
    assert exc_info.value.code == "confirmation_required"
    assert exc_info.value.http_status == 409


def test_confirmation_disabled_executes_directly(monkeypatch):
    """CONFIRMATION_ENABLED=false 时写工具直接执行，返回正常结果。"""
    monkeypatch.setattr(
        tools,
        "get_settings",
        lambda: SimpleNamespace(confirmation_enabled=False),
    )
    monkeypatch.setattr(
        tools,
        "_apply_refund_impl",
        lambda order_id, reason: {"found": True, "success": True, "message": "ok"},
    )

    result = tools.apply_refund("20261103001", "test")
    assert result["success"] is True
    assert "needs_confirmation" not in result


def test_stream_ndjson_structure_unchanged(monkeypatch):
    """/chat/stream 的 NDJSON 结构不变（不新增 confirmation 事件）。"""
    response = _post(
        "/chat/stream",
        {"session_id": "sess-stream", "message": "你好"},
        "confirm-stream-01",
    )
    assert response.status_code == 200

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    event_names = [line.get("event") for line in lines if line.get("event")]
    assert event_names == ["start", "done"]
    assert any("delta" in line for line in lines)

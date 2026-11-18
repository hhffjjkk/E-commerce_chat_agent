"""流式 route 事件接入测试：顺序、字段、pending 粘滞与 Router 异常回退。"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.harness import selection as selection_module  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import get_guardrails, reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import get_pending_store, reset_pending_store  # noqa: E402
from agent_system.harness.router import RoutePolicy, reset_router  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    """隔离会话库、Router、Guardrails 与 Profile。"""
    from agent_system.memory import SessionStore

    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    reset_guardrails()
    reset_pending_store()
    original_store = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        reset_router()
        reset_guardrails()
        reset_pending_store()
        reset_profile_registry()
        register_builtin_profiles()


def _post(path, body, request_id="stream-route-01"):
    """调用端点的通用 helper。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def _stream_lines(response):
    """解析 NDJSON。"""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_order_and_route_event():
    """NDJSON 顺序为 start -> route -> delta -> done，route 字段正确。"""
    response = _post(
        "/chat/stream",
        {"session_id": "s-stream", "message": "我的订单到哪了"},
    )
    assert response.status_code == 200
    lines = _stream_lines(response)

    event_names = [line.get("event") for line in lines if line.get("event")]
    assert event_names == ["start", "route", "done"]
    assert lines[0]["event"] == "start"
    assert lines[1]["event"] == "route"

    route = lines[1]
    for field in ("event", "request_id", "ts", "seq", "agent", "confidence", "reason"):
        assert field in route
    assert route["agent"] == "order"
    assert route["display_name"] == "订单与物流"
    assert route["strategy"] == "rule"
    assert route["reason"] == "router"
    assert any("delta" in line for line in lines)


def test_stream_pending_override():
    """存在 pending 时 route 为 pending_override / after_sales。"""
    get_guardrails().intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "s-stream-pending"
    )
    response = _post(
        "/chat/stream",
        {"session_id": "s-stream-pending", "message": "确认"},
    )
    assert response.status_code == 200
    route = [line for line in _stream_lines(response) if line.get("event") == "route"][0]
    assert route["agent"] == "after_sales"
    assert route["reason"] == "pending_override"
    assert route["strategy"] == "pending_override"
    assert route["confidence"] == 1.0


def test_stream_schema_has_new_optional_fields():
    """schema 端点的 route 可选字段包含 display_name / strategy。"""

    async def get_schema():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/chat/stream/schema", headers={"X-Request-ID": "stream-schema-02"})

    response = asyncio.run(get_schema())
    assert response.status_code == 200
    route_schema = response.json()["events"]["route"]
    assert "display_name" in route_schema["optional"]
    assert "strategy" in route_schema["optional"]
    # 必填字段保持不变
    assert route_schema["required"] == ["event", "request_id", "ts", "seq"]


def test_stream_router_exception_falls_back(monkeypatch):
    """Router 抛异常时仍能完成流，route 为 router_exception。"""

    def boom(message):
        raise RuntimeError("router down")

    fake_router = type(
        "R",
        (),
        {"policy": RoutePolicy(), "route": staticmethod(boom)},
    )()
    monkeypatch.setattr(selection_module, "get_router", lambda: fake_router)

    response = _post(
        "/chat/stream",
        {"session_id": "s-stream-error", "message": "你好"},
        "stream-route-err",
    )
    assert response.status_code == 200
    lines = _stream_lines(response)
    route = [line for line in lines if line.get("event") == "route"][0]
    assert route["agent"] == "pre_sales"
    assert route["reason"] == "router_exception"
    assert route["strategy"] == "fallback"
    assert [line.get("event") for line in lines if line.get("event")] == [
        "start",
        "route",
        "done",
    ]


def test_stream_mock_path_emits_route():
    """Mock 路径（无 API Key）也发 route 事件。"""
    response = _post("/chat/stream", {"session_id": "s-mock", "message": "这个商品多少钱"})
    route = [line for line in _stream_lines(response) if line.get("event") == "route"][0]
    assert route["agent"] == "pre_sales"
    assert route["strategy"] == "rule"

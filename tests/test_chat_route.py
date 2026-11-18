"""HTTP 路由接入测试：/chat route 字段、pending 粘滞与 webhook。"""

import asyncio
import json
import logging
import os
import sys
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
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import get_guardrails, reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import reset_pending_store  # noqa: E402
from agent_system.harness.router import reset_router  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """收集结构化日志。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """挂载 JSON handler。"""
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


def _post(path, body, request_id="route-chat-0001"):
    """调用 HTTP 端点。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def _events(handler):
    """解析日志。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def test_chat_route_order(captured_logs):
    """订单消息路由到 order，strategy=rule。"""
    response = _post(
        "/chat",
        {"session_id": "s-order", "message": "我的订单到哪了"},
        "route-order-0001",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route"]["agent"] == "order"
    assert body["route"]["display_name"] == "订单与物流"
    assert body["route"]["strategy"] == "rule"
    assert body["route"]["reason"] == "router"
    assert body["reply"]


def test_chat_route_after_sales():
    """退款消息路由到 after_sales。"""
    response = _post(
        "/chat",
        {"session_id": "s-refund", "message": "我要退款"},
        "route-refund-001",
    )
    assert response.status_code == 200
    assert response.json()["route"]["agent"] == "after_sales"


def test_chat_pending_override():
    """存在 pending 时“确认”消息强制 after_sales。"""
    get_guardrails().intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "s-pending"
    )
    response = _post(
        "/chat",
        {"session_id": "s-pending", "message": "确认"},
        "route-pending-01",
    )

    assert response.status_code == 200
    route = response.json()["route"]
    assert route["agent"] == "after_sales"
    assert route["reason"] == "pending_override"
    assert route["strategy"] == "pending_override"
    assert route["confidence"] == 1.0


def test_chat_reply_unchanged_and_route_safe():
    """reply 仍是业务回复；route 不含用户消息原文。"""
    message = "我的订单到哪了"
    response = _post(
        "/chat",
        {"session_id": "s-safe", "message": message},
        "route-safe-0001",
    )
    body = response.json()
    assert message not in body["reply"]
    assert message not in json.dumps(body["route"], ensure_ascii=False)
    # 旧客户端只取 reply 也能工作
    assert isinstance(body["reply"], str) and body["reply"]


def test_webhook_routes_and_keeps_response_shape(captured_logs):
    """webhook 触发路由，响应结构保持 session_id/reply。"""
    response = _post(
        "/webhook/generic",
        {"session_id": "s-hook", "message": "我的订单到哪了"},
        "route-hook-0001",
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"session_id", "reply"}
    assert body["session_id"] == "s-hook"

    selected = [e for e in _events(captured_logs) if e["event"] == "route_selected"]
    assert selected and selected[-1]["agent"] == "order"

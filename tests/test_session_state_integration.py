"""会话状态缓存接入测试：/chat 与 /chat/stream 旁路记录最近路由。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.cache import (  # noqa: E402
    get_session_state_cache,
    reset_redis_manager,
    reset_session_state_cache,
)
from agent_system.config import get_settings  # noqa: E402
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
def clean_state(tmp_path, monkeypatch):
    """隔离会话库、Profile、Router、Guardrails 与缓存单例。

    本文件只验证会话状态缓存：pending 后端强制 memory，
    避免 REDIS_URL 存在时被步骤 3 的 RedisPendingStore 选中而干扰断言。
    """
    from agent_system.memory import SessionStore

    monkeypatch.setenv("PENDING_STORE_BACKEND", "memory")
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    reset_guardrails()
    reset_pending_store()
    reset_session_state_cache()
    reset_redis_manager()
    get_settings.cache_clear()

    original_store = app_module.store
    original_cache = app_module.state_cache
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        app_module.state_cache = original_cache
        reset_router()
        reset_guardrails()
        reset_pending_store()
        reset_profile_registry()
        register_builtin_profiles()
        reset_session_state_cache()
        reset_redis_manager()
        get_settings.cache_clear()


class _FakeHashRedis:
    """最小 HASH Redis 替身。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.hashes: dict[str, dict] = {}

    def ping(self):
        if self.error is not None:
            raise self.error
        return True

    def hset(self, key, mapping=None):
        if self.error is not None:
            raise self.error
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    def hgetall(self, key):
        if self.error is not None:
            raise self.error
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        if self.error is not None:
            raise self.error
        return True

    def delete(self, key):
        if self.error is not None:
            raise self.error
        self.hashes.pop(key, None)
        return 1

    def close(self):
        pass


@pytest.fixture
def state_cache(monkeypatch):
    """启用假 Redis，并把 app 的 state_cache 指向新的 SessionStateCache。"""
    fake = _FakeHashRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_session_state_cache()
    monkeypatch.setattr(app_module, "state_cache", get_session_state_cache())
    return fake


@pytest.fixture
def broken_state_cache(monkeypatch):
    """启用假 Redis 但让它不可达，用于验证旁路失败不影响响应。"""
    fake = _FakeHashRedis(error=ConnectionError("boom"))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_session_state_cache()
    monkeypatch.setattr(app_module, "state_cache", get_session_state_cache())
    return fake


def _post(path, body, request_id):
    """通过 ASGITransport 发起 POST。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def _events(handler):
    """解析日志记录。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def test_chat_records_state_and_keeps_route_shape(state_cache):
    """/chat 写入最近路由，响应 route 键集保持不变。"""
    response = _post(
        "/chat",
        {"session_id": "p5s2-chat", "message": "我的订单到哪了"},
        "p5s2-chat-01",
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["route"]) == {"agent", "display_name", "strategy", "confidence", "reason"}

    stored = state_cache.hashes["session_state:p5s2-chat"]
    assert stored["last_route_agent"] == body["route"]["agent"] == "order"
    assert stored["last_route_strategy"] == body["route"]["strategy"] == "rule"
    assert float(stored["last_route_confidence"]) == pytest.approx(body["route"]["confidence"])


def test_stream_records_state_without_protocol_change(state_cache):
    """/chat/stream 同样写入状态，NDJSON 仍是 start/route/delta/done。"""
    response = _post(
        "/chat/stream",
        {"session_id": "p5s2-stream", "message": "我要退款"},
        "p5s2-stream-01",
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0]["event"] == "start"
    assert any(line.get("event") == "route" for line in lines)
    assert any("delta" in line for line in lines)
    assert any(line.get("event") == "done" for line in lines)
    assert not [line for line in lines if line.get("event") == "error"]

    stored = state_cache.hashes["session_state:p5s2-stream"]
    assert stored["last_route_agent"] == "after_sales"
    assert stored["last_route_strategy"] == "rule"


def test_pending_override_written(state_cache):
    """pending 粘滞路径写入 pending_override / 1.0。"""
    get_guardrails().intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "p5s2-pending"
    )

    response = _post(
        "/chat",
        {"session_id": "p5s2-pending", "message": "确认"},
        "p5s2-pending-01",
    )

    assert response.status_code == 200
    assert response.json()["route"]["strategy"] == "pending_override"
    stored = state_cache.hashes["session_state:p5s2-pending"]
    assert stored["last_route_strategy"] == "pending_override"
    assert float(stored["last_route_confidence"]) == 1.0


def test_chat_survives_cache_failure(broken_state_cache, captured_logs):
    """Redis 不可达：/chat 仍 200，route 结构不变，且记录写失败事件。"""
    response = _post(
        "/chat",
        {"session_id": "p5s2-broken", "message": "我的订单到哪了"},
        "p5s2-broken-01",
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body["route"]) == {"agent", "display_name", "strategy", "confidence", "reason"}
    assert body["route"]["agent"] == "order"

    events = [e for e in _events(captured_logs) if e["event"] == "session_state_write_failed"]
    assert events
    assert events[-1]["op"] == "set_route"
    assert events[-1]["reason"] == "connect_error"


def test_stream_survives_cache_failure(broken_state_cache, captured_logs):
    """Redis 不可达：/chat/stream 仍正常输出，不出现 error 事件。"""
    response = _post(
        "/chat/stream",
        {"session_id": "p5s2-broken-stream", "message": "我的订单到哪了"},
        "p5s2-broken-02",
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert any("delta" in line for line in lines)
    assert not [line for line in lines if line.get("event") == "error"]

    events = [e for e in _events(captured_logs) if e["event"] == "session_state_write_failed"]
    assert events

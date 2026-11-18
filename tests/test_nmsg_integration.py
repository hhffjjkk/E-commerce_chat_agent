"""归一化消息缓存接入测试：/chat 与 /chat/stream 的命中、写回与跳过。"""

import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "REDIS_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.cache import (  # noqa: E402
    get_nmsg_cache,
    reset_nmsg_cache,
    reset_redis_manager,
)
from agent_system.config import get_settings  # noqa: E402
from agent_system.harness.agent_profile import (  # noqa: E402
    get_profile_registry,
    reset_profile_registry,
)
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import reset_guardrails  # noqa: E402
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


class FakeRedis:
    """最小 STRING 客户端。"""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.error: Exception | None = None

    def _check(self):
        if self.error is not None:
            raise self.error

    def ping(self):
        self._check()
        return True

    def close(self):
        pass

    def get(self, key):
        self._check()
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self._check()
        self.values[key] = value
        return True

    def delete(self, key):
        self._check()
        self.values.pop(key, None)
        return 1


class FakeBot:
    """可控制回复、工具记录与“是否应被调用”的假 bot。"""

    def __init__(self, reply_text="极光笔记本有货，价格 5999 元", used_tools=None):
        self.reply_text = reply_text
        self._last_used_tools = list(used_tools or [])
        self.calls = 0
        self.fail_if_called = False

    def reply(self, message, history=None, *, profile=None):
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("缓存命中时不应调用 bot.reply")
        return self.reply_text

    def reply_stream(self, message, history=None, *, profile=None):
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("缓存命中时不应调用 bot.reply_stream")
        yield self.reply_text


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """隔离会话库、Profile、Router、Guardrails 与缓存单例。"""
    from agent_system.memory import SessionStore

    monkeypatch.setenv("PENDING_STORE_BACKEND", "memory")
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    reset_guardrails()
    reset_pending_store()
    reset_nmsg_cache()
    reset_redis_manager()
    get_settings.cache_clear()

    original_store = app_module.store
    original_bot = app_module.bot
    original_cache = app_module.nmsg_cache
    original_settings = app_module.settings
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        app_module.bot = original_bot
        app_module.nmsg_cache = original_cache
        app_module.settings = original_settings
        reset_router()
        reset_guardrails()
        reset_pending_store()
        reset_profile_registry()
        register_builtin_profiles()
        reset_nmsg_cache()
        reset_redis_manager()
        get_settings.cache_clear()


@pytest.fixture
def cache_on(monkeypatch):
    """开启缓存并注入假 Redis；返回假客户端。"""
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_nmsg_cache()
    monkeypatch.setattr(app_module, "nmsg_cache", get_nmsg_cache())
    monkeypatch.setattr(
        app_module,
        "settings",
        replace(app_module.settings, nmsg_cache_enabled=True),
    )
    return fake


def _post(path, body, request_id):
    """通过 ASGITransport 发起 POST。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def _events(handler):
    return [json.loads(handler.format(record)) for record in handler.records]


def test_disabled_cache_keeps_old_behavior(captured_logs):
    """默认关闭：/chat 无 cache 字段，流无 cache 事件。"""
    response = _post(
        "/chat", {"session_id": "nmsg-off", "message": "这个商品多少钱"}, "nmsg-off-01"
    )
    assert response.status_code == 200
    assert "cache" not in response.json()

    stream = _post(
        "/chat/stream",
        {"session_id": "nmsg-off", "message": "这个商品多少钱"},
        "nmsg-off-02",
    )
    lines = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
    assert not [line for line in lines if line.get("event") == "cache"]
    assert [e for e in _events(captured_logs) if e["event"].startswith("cache_")] == []


def test_chat_miss_then_hit(cache_on):
    """首次未命中并写回；第二次同消息命中且不调用 LLM。"""
    app_module.bot = FakeBot()
    message = "这个商品多少钱"

    first = _post("/chat", {"session_id": "nmsg-hit", "message": message}, "nmsg-hit-01")
    assert first.status_code == 200
    assert first.json()["cache"] == {"hit": False, "layer": "nmsg"}

    app_module.bot.fail_if_called = True
    second = _post("/chat", {"session_id": "nmsg-hit", "message": message}, "nmsg-hit-02")
    assert second.status_code == 200
    body = second.json()
    assert body["cache"] == {"hit": True, "layer": "nmsg"}
    assert body["reply"] == first.json()["reply"]
    assert app_module.bot.calls == 1


def test_stream_hit_emits_cache_and_deltas(cache_on):
    """流式命中：start → route → cache(hit=true) → delta... → done。"""
    app_module.bot = FakeBot()
    message = "这个商品多少钱"
    _post("/chat", {"session_id": "nmsg-stream", "message": message}, "nmsg-stream-00")

    app_module.bot.fail_if_called = True
    stream = _post(
        "/chat/stream",
        {"session_id": "nmsg-stream", "message": message},
        "nmsg-stream-01",
    )
    assert stream.status_code == 200
    lines = [json.loads(line) for line in stream.text.splitlines() if line.strip()]

    names = [line.get("event") for line in lines if line.get("event")]
    assert names[:3] == ["start", "route", "cache"]
    cache_event = next(line for line in lines if line.get("event") == "cache")
    assert cache_event["hit"] is True
    assert cache_event["layer"] == "nmsg"
    assert "key_prefix" not in cache_event

    text = "".join(line["delta"] for line in lines if "delta" in line)
    assert text == app_module.bot.reply_text


def test_after_sales_not_cached(cache_on, captured_logs):
    """after_sales 不在白名单：不写回并记录 profile_not_in_whitelist。"""
    app_module.bot = FakeBot("已为您创建退款待确认记录")

    response = _post(
        "/chat", {"session_id": "nmsg-after", "message": "我要退款"}, "nmsg-after-01"
    )

    assert response.status_code == 200
    assert response.json()["cache"] == {"hit": False, "layer": "nmsg"}
    assert cache_on.values == {}
    skipped = [e for e in _events(captured_logs) if e["event"] == "cache_write_skipped"]
    assert skipped and skipped[-1]["reason"] == "profile_not_in_whitelist"


def test_write_tool_not_cached(cache_on, captured_logs):
    """调用过 write 工具：写回被跳过。"""
    app_module.bot = FakeBot(used_tools=["apply_refund"])

    response = _post(
        "/chat", {"session_id": "nmsg-write", "message": "这个商品多少钱"}, "nmsg-write-01"
    )

    assert response.status_code == 200
    assert cache_on.values == {}
    skipped = [e for e in _events(captured_logs) if e["event"] == "cache_write_skipped"]
    assert skipped and skipped[-1]["reason"] == "write_tool_used"


def test_internal_tool_not_cached(cache_on, captured_logs):
    """调用过 internal 工具：写回被跳过。"""
    app_module.bot = FakeBot(used_tools=["confirm_action"])

    _post(
        "/chat", {"session_id": "nmsg-internal", "message": "这个商品多少钱"}, "nmsg-internal-01"
    )

    skipped = [e for e in _events(captured_logs) if e["event"] == "cache_write_skipped"]
    assert skipped and skipped[-1]["reason"] == "internal_tool_used"


def test_short_message_not_cached(cache_on, captured_logs):
    """短消息不写回。"""
    app_module.bot = FakeBot("您好")

    _post("/chat", {"session_id": "nmsg-short", "message": "好"}, "nmsg-short-01")

    assert cache_on.values == {}
    skipped = [e for e in _events(captured_logs) if e["event"] == "cache_write_skipped"]
    assert skipped and skipped[-1]["reason"] == "not_cacheable"


def test_redis_failure_does_not_break_requests(cache_on, captured_logs):
    """Redis 故障：请求仍 200，记录 get/put 失败事件。"""
    app_module.bot = FakeBot()
    cache_on.error = ConnectionError("boom")

    response = _post(
        "/chat", {"session_id": "nmsg-down", "message": "这个商品多少钱"}, "nmsg-down-01"
    )
    assert response.status_code == 200
    assert response.json()["cache"] == {"hit": False, "layer": "nmsg"}

    stream = _post(
        "/chat/stream",
        {"session_id": "nmsg-down", "message": "这个商品多少钱"},
        "nmsg-down-02",
    )
    assert stream.status_code == 200
    lines = [json.loads(line) for line in stream.text.splitlines() if line.strip()]
    assert not [line for line in lines if line.get("event") == "error"]
    assert any(line.get("event") == "cache" and line["hit"] is False for line in lines)

    names = {e["event"] for e in _events(captured_logs)}
    assert "cache_get_failed" in names
    assert "cache_put_failed" in names


def test_stable_prefix_hash_isolated_by_profile():
    """pre_sales 与 order 的稳定前缀哈希不同，不会互相命中。"""
    pre_sales = get_profile_registry().get("pre_sales")
    order = get_profile_registry().get("order")

    pre_hash = app_module._resolve_stable_prefix_hash(pre_sales)
    order_hash = app_module._resolve_stable_prefix_hash(order)

    assert pre_hash != order_hash
    assert app_module._nmsg_ttl_for(pre_sales) == app_module.settings.nmsg_ttl_seconds
    assert app_module._nmsg_ttl_for(order) == app_module.settings.nmsg_order_ttl_seconds

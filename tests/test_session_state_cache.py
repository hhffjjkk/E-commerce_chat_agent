"""SessionStateCache 单测：存储、TTL、前缀、降级与隐私字段。"""

import json
import logging
import os
import sys
from pathlib import Path

import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.cache import (  # noqa: E402
    get_session_state_cache,
    reset_redis_manager,
    reset_session_state_cache,
)
from agent_system.config import get_settings  # noqa: E402
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
def isolate_state(monkeypatch):
    """重置单例与配置缓存，避免环境变量相互影响。"""
    reset_session_state_cache()
    reset_redis_manager()
    get_settings.cache_clear()
    yield
    reset_session_state_cache()
    reset_redis_manager()
    get_settings.cache_clear()


class _FakeHashRedis:
    """最小 HASH Redis 替身：覆盖 hset/hgetall/expire/delete 与探活。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.hashes: dict[str, dict] = {}
        self.hset_calls: list[tuple[str, dict]] = []
        self.expires: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.closed = False

    def ping(self):
        if self.error is not None:
            raise self.error
        return True

    def hset(self, key, mapping=None):
        if self.error is not None:
            raise self.error
        self.hset_calls.append((key, dict(mapping or {})))
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    def hgetall(self, key):
        if self.error is not None:
            raise self.error
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        if self.error is not None:
            raise self.error
        self.expires.append((key, int(ttl)))
        return True

    def delete(self, key):
        if self.error is not None:
            raise self.error
        self.deleted.append(key)
        self.hashes.pop(key, None)
        return 1

    def close(self):
        self.closed = True


def _events(handler: ListHandler) -> list[dict]:
    """解析日志记录。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _enable(monkeypatch, fake, *, prefix=None, ttl=None):
    """配置 Redis 环境并返回 SessionStateCache 单例。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    if prefix is not None:
        monkeypatch.setenv("REDIS_KEY_PREFIX", prefix)
    if ttl is not None:
        monkeypatch.setenv("REDIS_SESSION_STATE_TTL_SECONDS", str(ttl))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_session_state_cache()
    return get_session_state_cache()


def test_disabled_redis_is_noop(monkeypatch, captured_logs):
    """未配置 Redis：读写全部 no-op，且不产生失败噪音。"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    cache = get_session_state_cache()

    assert cache.get("s1") is None
    cache.set_route(
        "s1",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.67,
        reason="router",
    )
    cache.touch("s1")
    cache.delete("s1")

    assert [e for e in _events(captured_logs) if e["event"] == "session_state_write_failed"] == []


def test_set_get_touch_delete(monkeypatch):
    """可达时写入/读回字段完整，touch 只更新活跃时间，delete 后为空。"""
    fake = _FakeHashRedis()
    cache = _enable(monkeypatch, fake, ttl=120)

    cache.set_route(
        "s1",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.6667,
        reason="router",
    )
    assert fake.hset_calls[-1][0] == "session_state:s1"
    assert fake.expires[-1] == ("session_state:s1", 120)

    data = cache.get("s1")
    assert data["last_route_agent"] == "order"
    assert data["last_route_display_name"] == "订单与物流"
    assert data["last_route_strategy"] == "rule"
    assert data["last_route_confidence"] == pytest.approx(0.6667)
    assert data["last_route_reason"] == "router"
    assert isinstance(data["last_route_at"], int)
    assert isinstance(data["last_activity_at"], int)

    cache.touch("s1")
    assert set(fake.hset_calls[-1][1]) == {"last_activity_at"}

    cache.delete("s1")
    assert fake.deleted == ["session_state:s1"]
    assert cache.get("s1") is None


def test_confidence_none_is_omitted(monkeypatch):
    """confidence=None 时不写入该字段，读回为 None。"""
    fake = _FakeHashRedis()
    cache = _enable(monkeypatch, fake)

    cache.set_route(
        "s1",
        agent="pre_sales",
        display_name="售前咨询",
        strategy="fallback",
        confidence=None,
        reason="router",
    )

    assert "last_route_confidence" not in fake.hset_calls[-1][1]
    assert cache.get("s1")["last_route_confidence"] is None


def test_key_prefix(monkeypatch):
    """多租户前缀：默认空与 tenantA 两种 key 形态。"""
    fake = _FakeHashRedis()
    cache = _enable(monkeypatch, fake)
    cache.set_route(
        "s1",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.5,
        reason="router",
    )
    assert fake.hset_calls[-1][0] == "session_state:s1"

    fake2 = _FakeHashRedis()
    cache2 = _enable(monkeypatch, fake2, prefix="tenantA")
    cache2.set_route(
        "s1",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.5,
        reason="router",
    )
    assert fake2.hset_calls[-1][0] == "tenantA:session_state:s1"


def test_unreachable_reads_miss_and_logs_write_failure(monkeypatch, captured_logs):
    """已配置但不可达：读返回 None，写静默失败并记录一次事件（哈希 ID）。"""
    fake = _FakeHashRedis(error=ConnectionError("boom"))
    cache = _enable(monkeypatch, fake)

    assert cache.get("session-raw-123") is None
    cache.set_route(
        "session-raw-123",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.5,
        reason="router",
    )

    events = [e for e in _events(captured_logs) if e["event"] == "session_state_write_failed"]
    assert len(events) == 1
    assert events[0]["op"] == "set_route"
    assert events[0]["reason"] == "connect_error"
    assert len(events[0]["session_id_hash"]) == 8
    # 隐私：事件中不得出现原始 session_id
    assert "session-raw-123" not in json.dumps(events[0], ensure_ascii=False)


def test_singleton_and_reset(monkeypatch):
    """单例：多次获取同一实例，reset 后可重建。"""
    fake = _FakeHashRedis()
    _enable(monkeypatch, fake)

    first = get_session_state_cache()
    assert get_session_state_cache() is first

    reset_session_state_cache()
    second = get_session_state_cache()
    assert second is not first


def test_success_event_is_opt_in(monkeypatch, captured_logs):
    """成功事件默认不记；打开 SESSION_STATE_LOG_SUCCESS 后才出现。"""
    fake = _FakeHashRedis()
    cache = _enable(monkeypatch, fake)
    cache.set_route(
        "s1",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.5,
        reason="router",
    )
    assert [e for e in _events(captured_logs) if e["event"] == "session_state_updated"] == []

    monkeypatch.setenv("SESSION_STATE_LOG_SUCCESS", "true")
    cache2 = _enable(monkeypatch, fake)
    cache2.set_route(
        "s2",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        confidence=0.5,
        reason="router",
    )
    updated = [e for e in _events(captured_logs) if e["event"] == "session_state_updated"]
    assert updated and updated[-1]["agent"] == "order"
    assert len(updated[-1]["session_id_hash"]) == 8

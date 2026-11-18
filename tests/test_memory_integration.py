"""memory 与 RedisManager 集成测试：create_store 选择、回退与 key 格式不变。"""

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

from agent_system import memory  # noqa: E402
from agent_system.cache import RedisUnavailable, reset_redis_manager  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.memory import RedisSessionStore, SessionStore, create_store  # noqa: E402
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
def isolate_state(monkeypatch, tmp_path):
    """重置单例/配置缓存，并把 SQLite 落到临时目录。"""
    reset_redis_manager()
    get_settings.cache_clear()
    monkeypatch.setattr(memory, "DEFAULT_DB", tmp_path / "sessions.db")
    yield
    reset_redis_manager()
    get_settings.cache_clear()


class _FakeListRedis:
    """最小 Redis 替身：覆盖会话存储用到的 list/delete 与探活。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.expires: list[tuple[str, int]] = []
        self.closed = False

    def ping(self):
        if self.error is not None:
            raise self.error
        return True

    def lrange(self, key, start, end):
        self.calls.append(("lrange", key))
        return list(self.lists.get(key, []))

    def rpush(self, key, value):
        self.calls.append(("rpush", key))
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        self.calls.append(("ltrim", key))

    def expire(self, key, ttl):
        self.expires.append((key, int(ttl)))
        return True

    def delete(self, key):
        self.calls.append(("delete", key))
        self.lists.pop(key, None)

    def close(self):
        self.closed = True


def _events(handler: ListHandler) -> list[dict]:
    """解析日志记录。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def test_create_store_sqlite_when_redis_not_configured(monkeypatch, captured_logs):
    """未配置 REDIS_URL：返回 SQLite，行为与历史一致。"""
    monkeypatch.delenv("REDIS_URL", raising=False)

    store = create_store()

    assert isinstance(store, SessionStore)
    store.append("s1", "user", "你好")
    assert store.get("s1") == [{"role": "user", "content": "你好"}]

    events = [e for e in _events(captured_logs) if e["event"] == "store_selected"]
    assert events and events[-1]["backend"] == "sqlite"
    assert events[-1]["reason"] == "redis_not_configured"


def test_create_store_redis_reachable_keeps_semantics(monkeypatch):
    """配置且可达：返回 RedisSessionStore，key 格式与读写语义不变。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    assert isinstance(store, RedisSessionStore)

    # key 格式快照：仍为 chat:{session_id}
    assert store.get("s1") == []
    assert fake.calls[-1] == ("lrange", "chat:s1")

    store.append("s1", "user", "你好")
    assert fake.lists["chat:s1"] == [
        json.dumps({"role": "user", "content": "你好"}, ensure_ascii=False)
    ]
    assert ("rpush", "chat:s1") in fake.calls
    assert ("ltrim", "chat:s1") in fake.calls

    store.clear("s1")
    assert ("delete", "chat:s1") in fake.calls


def test_create_store_raises_when_unreachable_without_fallback(monkeypatch):
    """配置但不可达且未开启回退：抛 RedisUnavailable，不静默写 SQLite。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setenv("REDIS_FALLBACK_TO_MEMORY", "false")
    fake = _FakeListRedis(error=ConnectionError("boom"))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    with pytest.raises(RedisUnavailable) as exc_info:
        create_store()

    assert str(exc_info.value) == "connect_error"


def test_create_store_falls_back_when_enabled(monkeypatch, captured_logs):
    """显式开启回退：返回 SQLite，并记录一次 redis_fallback。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    monkeypatch.setenv("REDIS_FALLBACK_TO_MEMORY", "true")
    fake = _FakeListRedis(error=TimeoutError("timeout"))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()

    assert isinstance(store, SessionStore)
    events = _events(captured_logs)
    fallbacks = [e for e in events if e["event"] == "redis_fallback"]
    assert len(fallbacks) == 1
    assert fallbacks[0]["reason"] == "timeout"
    assert fallbacks[0]["fallback_to"] == "sqlite"

    selected = [e for e in events if e["event"] == "store_selected"]
    assert selected and selected[-1]["backend"] == "sqlite"


def test_session_ttl_zero_does_not_expire(monkeypatch):
    """默认 TTL=0：append 后不调用 expire，保持既有持久化行为。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    store.append("s1", "user", "你好")

    assert fake.expires == []


def test_session_ttl_applied_on_append(monkeypatch):
    """配置 TTL：append 后按配置值调用 expire。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_SESSION_TTL_SECONDS", "60")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    store.append("s1", "user", "你好")

    assert fake.expires == [("chat:s1", 60)]


def test_get_does_not_refresh_ttl(monkeypatch):
    """读取不续期：get 不触发 expire 调用。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_SESSION_TTL_SECONDS", "60")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    store.append("s1", "user", "你好")
    before = list(fake.expires)

    store.get("s1")

    assert fake.expires == before


def test_key_prefix_applies_to_history(monkeypatch):
    """多租户前缀：历史消息 key 变为 tenantA:chat:{sid}。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_KEY_PREFIX", "tenantA")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    store.append("s1", "user", "你好")
    store.get("s1")

    assert ("rpush", "tenantA:chat:s1") in fake.calls
    assert ("lrange", "tenantA:chat:s1") in fake.calls


def test_invalid_prefix_falls_back_and_logs(monkeypatch, captured_logs):
    """非法前缀回退为空串并记录 config_invalid（只记变量名）。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_KEY_PREFIX", "bad/prefix!")
    fake = _FakeListRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    store = create_store()
    store.append("s1", "user", "你好")

    assert ("rpush", "chat:s1") in fake.calls
    invalid = [e for e in _events(captured_logs) if e["event"] == "config_invalid"]
    assert invalid and "REDIS_KEY_PREFIX" in invalid[-1]["invalid_env"]
    # 只记录变量名，不把错误值写进日志
    assert "bad/prefix!" not in json.dumps(invalid[-1], ensure_ascii=False)

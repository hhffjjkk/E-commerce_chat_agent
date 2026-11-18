"""RedisManager 单测：未启用、不可达、可达、连接参数、单例与事件。"""

import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.cache import (  # noqa: E402
    RedisUnavailable,
    get_redis_manager,
    reset_redis_manager,
)
from agent_system.config import get_settings  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收集到内存，便于断言 redis_* 事件。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """临时挂载 JSON handler，测试结束后恢复。"""
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
def isolate_manager(monkeypatch):
    """每个用例重置单例与配置缓存，避免环境变量/连接池串味。"""
    reset_redis_manager()
    get_settings.cache_clear()
    yield
    reset_redis_manager()
    get_settings.cache_clear()


class _FakeRedis:
    """最小 Redis 替身：只实现 ping/close 与连接参数快照。"""

    def __init__(self, *, ok: bool = True, error: Exception | None = None, kwargs=None):
        self.ok = ok
        self.error = error
        self.pings = 0
        self.closed = False
        # redis-py 的真实客户端把连接参数放在 connection_pool.connection_kwargs
        self.connection_pool = SimpleNamespace(connection_kwargs=kwargs or {})

    def ping(self):
        self.pings += 1
        if self.error is not None:
            raise self.error
        return self.ok

    def close(self):
        self.closed = True


def _events(handler: ListHandler) -> list[dict]:
    """把收集到的日志渲染成 JSON 后解析。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def test_disabled_when_no_url(monkeypatch):
    """未配置 REDIS_URL：未启用、get_client 返回 None、ping 恒 False 且不抛。"""
    monkeypatch.delenv("REDIS_URL", raising=False)

    manager = get_redis_manager()

    assert manager.is_enabled() is False
    assert manager.get_client() is None
    assert manager.ping() is False
    assert manager.ping(force=True) is False


def test_unreachable_raises_and_ping_is_cached(monkeypatch, captured_logs):
    """已启用但不可达：get_client 抛 RedisUnavailable；ping 不抛且受 TTL 缓存。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    fake = _FakeRedis(error=ConnectionError("boom"))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    manager = get_redis_manager()
    with pytest.raises(RedisUnavailable) as exc_info:
        manager.get_client()
    assert str(exc_info.value) == "connect_error"

    # 第一次 get_client 已触发一次真实 PING；接下来两次普通 ping 走缓存
    assert fake.pings == 1
    assert manager.ping() is False
    assert manager.ping() is False
    assert fake.pings == 1

    # 健康检查用的 force=True 必须绕过缓存
    assert manager.ping(force=True) is False
    assert fake.pings == 2

    events = [e for e in _events(captured_logs) if e["event"] == "redis_ping_failed"]
    assert len(events) == 2
    assert all(e["reason"] == "connect_error" for e in events)
    # 事件不包含连接串，避免泄露地址与凭据
    assert all("127.0.0.1" not in json.dumps(e, ensure_ascii=False) for e in events)


def test_reachable_returns_client_and_force_bypasses_cache(monkeypatch):
    """已启用且可达：get_client 返回客户端；force=True 绕过缓存。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    manager = get_redis_manager()

    assert manager.get_client() is fake
    assert fake.pings == 1
    assert manager.ping() is True
    assert fake.pings == 1
    assert manager.ping(force=True) is True
    assert fake.pings == 2


def test_connection_params_are_read_from_settings(monkeypatch):
    """连接池参数来自 REDIS_* 配置，decode_responses 保持 True。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "2.5")
    monkeypatch.setenv("REDIS_CONNECT_TIMEOUT", "3.5")
    monkeypatch.setenv("REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("REDIS_HEALTH_CHECK_INTERVAL", "11")
    captured = {}

    def _from_url(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _FakeRedis(kwargs=kwargs)

    monkeypatch.setattr(redis_module, "from_url", _from_url)

    manager = get_redis_manager()
    assert manager.get_client() is not None

    assert captured["url"] == "redis://127.0.0.1:6379/0"
    assert captured["decode_responses"] is True
    assert captured["socket_timeout"] == 2.5
    assert captured["socket_connect_timeout"] == 3.5
    assert captured["max_connections"] == 7
    assert captured["health_check_interval"] == 11


def test_singleton_and_reset_closes_pool(monkeypatch):
    """get_redis_manager 返回同一实例；reset 关闭旧连接池并重建。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    first = get_redis_manager()
    assert get_redis_manager() is first
    assert first.get_client() is fake

    reset_redis_manager()
    assert fake.closed is True

    second = get_redis_manager()
    assert second is not first
    assert second.is_enabled() is True

"""NmsgCache 单测：CRUD、TTL、前缀、隔离、降级与隐私。"""

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
    NmsgCache,
    get_nmsg_cache,
    reset_nmsg_cache,
    reset_redis_manager,
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
def isolate(monkeypatch):
    """重置单例与配置缓存。"""
    reset_nmsg_cache()
    reset_redis_manager()
    monkeypatch.delenv("REDIS_URL", raising=False)
    get_settings.cache_clear()
    yield
    reset_nmsg_cache()
    reset_redis_manager()
    get_settings.cache_clear()


class FakeRedis:
    """最小 STRING 客户端。"""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
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
        self.setex_calls.append((key, int(ttl), value))
        self.values[key] = value
        return True

    def delete(self, key):
        self._check()
        self.values.pop(key, None)
        return 1


def _enable(monkeypatch, fake, *, prefix=None):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    if prefix is not None:
        monkeypatch.setenv("REDIS_KEY_PREFIX", prefix)
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_nmsg_cache()


def _events(handler):
    return [json.loads(handler.format(record)) for record in handler.records]


def test_disabled_is_noop(monkeypatch, captured_logs):
    """未配置 Redis：读返回 None、写 False，且无失败噪音。"""
    cache = NmsgCache()

    assert cache.get("p1", "这个商品多少钱") is None
    assert cache.put("p1", "这个商品多少钱", "5999") is False
    cache.delete("p1", "这个商品多少钱")

    events = [
        e for e in _events(captured_logs) if e["event"] in {"cache_get_failed", "cache_put_failed"}
    ]
    assert events == []


def test_put_get_and_isolation(monkeypatch):
    """写入后可命中；不同前缀哈希/消息互不命中；归一化等价消息命中同一项。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake)
    cache = NmsgCache()

    assert cache.put("prefix-a", "这个商品多少钱？", "5999 元") is True
    assert cache.get("prefix-a", "这个商品多少钱？") == "5999 元"
    # 全角/半角问号归一化后相同 -> 命中
    assert cache.get("prefix-a", "这个商品多少钱?") == "5999 元"
    # 不同 stable_prefix_hash -> 不命中
    assert cache.get("prefix-b", "这个商品多少钱？") is None
    # 不同消息 -> 不命中
    assert cache.get("prefix-a", "这个商品有货吗？") is None


def test_ttl_and_override(monkeypatch):
    """默认 TTL 与显式覆盖都传给 SETEX。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake)
    cache = NmsgCache(ttl_seconds=3600, order_ttl_seconds=60)

    cache.put("p1", "这个商品多少钱？", "reply")
    cache.put("p1", "这个商品有货吗？", "reply", ttl_seconds=120)

    assert fake.setex_calls[0][1] == 3600
    assert fake.setex_calls[1][1] == 120


def test_prefix(monkeypatch):
    """多租户前缀进入 key。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake, prefix="tenantA")
    cache = NmsgCache(prefix="tenantA")

    cache.put("p1", "这个商品多少钱？", "reply")

    assert fake.setex_calls[0][0].startswith("tenantA:nmsg:p1:")


def test_key_is_hashed_without_raw_message(monkeypatch):
    """key 只含哈希，不出现用户消息原文。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake)
    cache = NmsgCache()

    cache.put("p1", "我的订单到哪了", "reply")

    key = fake.setex_calls[0][0]
    assert "我的订单" not in key
    suffix = key.rsplit(":", 1)[-1]
    assert len(suffix) == 16
    int(suffix, 16)  # 16 位 hex


def test_redis_failure_degrades(monkeypatch, captured_logs):
    """Redis 故障：读 None、写 False，并记录 get/put 失败事件。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake)
    cache = NmsgCache()
    fake.error = ConnectionError("boom")

    assert cache.get("p1", "这个商品多少钱？") is None
    assert cache.put("p1", "这个商品多少钱？", "reply") is False

    events = _events(captured_logs)
    failed = [e for e in events if e["event"] in {"cache_get_failed", "cache_put_failed"}]
    assert {e["event"] for e in failed} == {"cache_get_failed", "cache_put_failed"}
    assert all(e["reason"] == "connect_error" for e in failed)


def test_singleton_and_reset(monkeypatch):
    """单例缓存与 reset 重建。"""
    fake = FakeRedis()
    _enable(monkeypatch, fake)

    first = get_nmsg_cache()
    assert get_nmsg_cache() is first

    reset_nmsg_cache()
    assert get_nmsg_cache() is not first

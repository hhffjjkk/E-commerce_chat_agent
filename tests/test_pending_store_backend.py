"""pending 后端选择测试：auto/memory/redis、降级、单例与 Guardrails 行为一致性。"""

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

from agent_system import tools  # noqa: E402
from agent_system.cache import RedisUnavailable, reset_redis_manager  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.harness import pending_store as pending_store_module  # noqa: E402
from agent_system.harness.guardrails import Guardrails  # noqa: E402
from agent_system.harness.pending_store import (  # noqa: E402
    InMemoryPendingStore,
    get_pending_store,
    reset_pending_store,
)
from agent_system.harness.redis_pending_store import RedisPendingStore  # noqa: E402


class FakePipeline:
    """简化 pipeline：排队后按序执行。"""

    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.ops = []

    def watch(self, key):
        return True

    def unwatch(self):
        return True

    def multi(self):
        return self

    def reset(self):
        self.ops = []

    def _queue(self, name, *args, **kwargs):
        self.ops.append((name, args, kwargs))
        return self

    def hset(self, key, mapping=None, field=None, value=None):
        if isinstance(mapping, str) and field is not None and value is None:
            return self._queue("hset", key, field=mapping, value=field)
        return self._queue("hset", key, mapping=mapping, field=field, value=value)

    def hgetall(self, key):
        return self.redis.hgetall(key)

    def sadd(self, key, *members):
        return self._queue("sadd", key, *members)

    def srem(self, key, *members):
        return self._queue("srem", key, *members)

    def zadd(self, key, mapping):
        return self._queue("zadd", key, mapping)

    def zrem(self, key, *members):
        return self._queue("zrem", key, *members)

    def expire(self, key, ttl):
        return self._queue("expire", key, ttl)

    def delete(self, key):
        return self._queue("delete", key)

    def execute(self):
        results = [getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.ops]
        self.ops = []
        return results


class FakeRedis:
    """pending store 所需命令的 Redis 替身。"""

    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.zsets = {}
        self.error = None

    def _check(self):
        if self.error is not None:
            raise self.error

    def ping(self):
        self._check()
        return True

    def close(self):
        pass

    def exists(self, key):
        self._check()
        return 1 if key in self.hashes else 0

    def hset(self, key, mapping=None, field=None, value=None):
        self._check()
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        else:
            bucket[field] = value
        return 1

    def hgetall(self, key):
        self._check()
        return dict(self.hashes.get(key, {}))

    def sadd(self, key, *members):
        self._check()
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key):
        self._check()
        return set(self.sets.get(key, set()))

    def srem(self, key, *members):
        self._check()
        self.sets.get(key, set()).difference_update(members)
        return len(members)

    def zadd(self, key, mapping):
        self._check()
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, *members):
        self._check()
        for member in members:
            self.zsets.get(key, {}).pop(member, None)
        return len(members)

    def zrangebyscore(self, key, minimum, maximum):
        self._check()
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float(maximum)
        selected = sorted(
            (score, member)
            for member, score in self.zsets.get(key, {}).items()
            if low <= score <= high
        )
        return [member for _, member in selected]

    def expire(self, key, ttl):
        self._check()
        return True

    def delete(self, key):
        self._check()
        for bucket in (self.hashes, self.sets, self.zsets):
            bucket.pop(key, None)
        return 1

    def pipeline(self, transaction=True):
        return FakePipeline(self)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """重置全局状态与环境变量。"""
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("PENDING_STORE_BACKEND", "auto")
    yield
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()


@pytest.fixture
def events(monkeypatch):
    """收集 pending_store_selected 事件。"""
    captured = []

    def fake_log_event(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(pending_store_module, "log_event", fake_log_event)
    return captured


def _redis(monkeypatch, fake=None, *, backend="auto"):
    """配置 Redis 环境并返回假客户端。"""
    fake = fake or FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setenv("PENDING_STORE_BACKEND", backend)
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_pending_store()
    return fake


def test_auto_without_redis_uses_memory(events):
    """auto + 未配置 Redis：内存实现，记录 auto_default。"""
    store = get_pending_store()

    assert isinstance(store, InMemoryPendingStore)
    selected = [e for e in events if e["event"] == "pending_store_selected"]
    assert selected[-1]["backend"] == "memory"
    assert selected[-1]["reason"] == "auto_default"


def test_auto_with_redis_uses_redis(monkeypatch, events):
    """auto + Redis 可达：Redis 实现，reason=config。"""
    _redis(monkeypatch)

    store = get_pending_store()

    assert isinstance(store, RedisPendingStore)
    selected = [e for e in events if e["event"] == "pending_store_selected"]
    assert selected[-1]["backend"] == "redis"
    assert selected[-1]["reason"] == "config"


def test_auto_with_unreachable_redis_raises(monkeypatch):
    """auto + Redis 不可达：显式失败，不回退内存。"""
    fake = FakeRedis()
    fake.error = ConnectionError("boom")
    _redis(monkeypatch, fake)

    with pytest.raises(RedisUnavailable):
        get_pending_store()


def test_forced_memory_wins_even_with_redis(monkeypatch, events):
    """backend=memory：即使 Redis 可达也强制内存实现。"""
    _redis(monkeypatch, backend="memory")

    store = get_pending_store()

    assert isinstance(store, InMemoryPendingStore)
    selected = [e for e in events if e["event"] == "pending_store_selected"]
    assert selected[-1]["reason"] == "forced"


def test_forced_redis_unreachable_raises(monkeypatch):
    """backend=redis + 不可达：启动期显式失败。"""
    fake = FakeRedis()
    fake.error = ConnectionError("boom")
    _redis(monkeypatch, fake, backend="redis")

    with pytest.raises(RedisUnavailable):
        get_pending_store()


def test_singleton_and_reset(monkeypatch):
    """单例缓存与 reset 后重建。"""
    _redis(monkeypatch)
    first = get_pending_store()
    assert get_pending_store() is first

    reset_pending_store()
    second = get_pending_store()
    assert second is not first
    assert isinstance(second, RedisPendingStore)


def test_guardrails_behavior_matches_memory(monkeypatch):
    """同一组输入下，内存与 Redis 后端的 intercept/confirm/reject 结果一致。"""
    monkeypatch.setattr(
        tools,
        "_apply_refund_impl",
        lambda order_id, reason: {"found": True, "success": True, "message": "退款已提交"},
    )
    monkeypatch.setattr(
        tools,
        "_cancel_order_impl",
        lambda order_id: {"found": True, "success": True, "message": "订单已取消"},
    )
    fake = _redis(monkeypatch)
    redis_store = RedisPendingStore(ttl_seconds=3600)

    memory_guard = Guardrails(ttl_seconds=300, store=InMemoryPendingStore())
    redis_guard = Guardrails(ttl_seconds=300, store=redis_store)
    assert memory_guard.lock_factory is None
    assert redis_guard.lock_factory is None

    args = {"order_id": "20261103001", "reason": "尺寸不合适"}
    memory_payload = memory_guard.intercept_write("apply_refund", args, "parity-s1")
    redis_payload = redis_guard.intercept_write("apply_refund", args, "parity-s1")

    assert memory_payload["needs_confirmation"] is True
    assert redis_payload["needs_confirmation"] is True
    assert memory_payload["summary"] == redis_payload["summary"]

    memory_result = memory_guard.confirm(memory_payload["action_id"], "parity-s1")
    redis_result = redis_guard.confirm(redis_payload["action_id"], "parity-s1")
    assert memory_result["success"] is True
    assert redis_result["success"] is True
    assert memory_result["result"] == redis_result["result"]

    reject_mem = memory_guard.intercept_write("cancel_order", {"order_id": "20261103002"}, "parity-s2")
    reject_redis = redis_guard.intercept_write("cancel_order", {"order_id": "20261103002"}, "parity-s2")
    assert (
        memory_guard.reject(reject_mem["action_id"], "parity-s2")["status"]
        == redis_guard.reject(reject_redis["action_id"], "parity-s2")["status"]
        == "rejected"
    )
    # Redis 中终态记录仍在主记录里，但已从会话索引移除
    assert "cancel_order" in fake.hashes[f"pending:{reject_redis['action_id']}"]["tool_name"]

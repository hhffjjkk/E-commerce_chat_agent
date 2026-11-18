"""两种 pending 后端的协议等价性测试（墙钟语义下）。"""

import os
import sys
import time
from pathlib import Path

import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.cache import reset_redis_manager  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.harness.pending_store import (  # noqa: E402
    InMemoryPendingStore,
    PendingAction,
    reset_pending_store,
)
from agent_system.harness.redis_pending_store import RedisPendingStore  # noqa: E402


class FakePipeline:
    """最小 pipeline：排队执行。"""

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
    """协议等价性测试用的 Redis 替身。"""

    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.zsets = {}

    def ping(self):
        return True

    def close(self):
        pass

    def exists(self, key):
        return 1 if key in self.hashes else 0

    def hset(self, key, mapping=None, field=None, value=None):
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update(mapping)
        else:
            bucket[field] = value
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def srem(self, key, *members):
        self.sets.get(key, set()).difference_update(members)
        return len(members)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, *members):
        for member in members:
            self.zsets.get(key, {}).pop(member, None)
        return len(members)

    def zrangebyscore(self, key, minimum, maximum):
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float(maximum)
        selected = sorted(
            (score, member)
            for member, score in self.zsets.get(key, {}).items()
            if low <= score <= high
        )
        return [member for _, member in selected]

    def expire(self, key, ttl):
        return True

    def delete(self, key):
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
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CONFIRMATION_CLOCK_SKEW_SECONDS", raising=False)
    monkeypatch.setenv("PENDING_STORE_BACKEND", "memory")
    get_settings.cache_clear()
    yield
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()


def _pending(action_id="a1", session_id="s1", *, created_at=None, lifetime=60.0):
    base = time.time() if created_at is None else created_at
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        tool_name="apply_refund",
        args={"order_id": "20261103001", "reason": "测试"},
        summary="为订单 20261103001 申请退款，原因：测试",
        created_at=base,
        expires_at=base + lifetime,
    )


@pytest.fixture
def stores(monkeypatch):
    """返回 (内存, Redis) 两个后端，Redis 使用共享假客户端。"""
    fake = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()
    return InMemoryPendingStore(), RedisPendingStore(ttl_seconds=3600)


def test_create_is_idempotent_in_both_backends(stores):
    """重复 create 返回同一记录，且不新增条目。"""
    memory, redis_store = stores

    for store in (memory, redis_store):
        first = store.create(_pending("a1", "s1"))
        second = store.create(_pending("a1", "s1"))

        assert first.action_id == second.action_id == "a1"
        assert second.summary == first.summary
        assert len(store.get_by_session("s1")) == 1


def test_get_and_status_transitions_match(stores):
    """create → get → confirmed → executed 的返回结构一致。"""
    memory, redis_store = stores

    results = []
    for store in (memory, redis_store):
        store.create(_pending("a1", "s1"))
        pending = store.get("a1")
        assert pending.status == "pending"
        # 先取字符串值：内存实现复用同一对象，对象引用会随后续迁移变化
        confirmed_status = store.mark("a1", "confirmed").status
        executed = store.mark("a1", "executed", result={"success": True})
        results.append(
            (
                pending.args,
                pending.summary,
                confirmed_status,
                executed.status,
                executed.result,
            )
        )

    assert results[0] == results[1]


def test_illegal_transition_message_matches(stores):
    """pending → executed 直接迁移：两后端抛同一中文 ValueError。"""
    memory, redis_store = stores

    messages = []
    for store in (memory, redis_store):
        store.create(_pending("a1", "s1"))
        with pytest.raises(ValueError) as exc_info:
            store.mark("a1", "executed")
        messages.append(str(exc_info.value))

    assert messages[0] == messages[1]
    assert "非法状态迁移: pending -> executed" in messages[0]


def test_missing_action_message_matches(stores):
    """查询不存在的记录：两后端 KeyError 文案一致。"""
    memory, redis_store = stores

    messages = []
    for store in (memory, redis_store):
        with pytest.raises(KeyError) as exc_info:
            store.mark("missing", "confirmed")
        messages.append(str(exc_info.value))

    assert messages[0] == messages[1]


def test_session_isolation_matches(stores):
    """会话隔离：只返回本会话记录。"""
    memory, redis_store = stores

    for store in (memory, redis_store):
        store.create(_pending("a1", "s1"))
        store.create(_pending("a2", "s2"))
        assert [p.action_id for p in store.get_by_session("s1")] == ["a1"]
        assert [p.action_id for p in store.get_by_session("s2")] == ["a2"]


def test_purge_expired_matches(stores):
    """同一 now 下，两后端清理条数与状态一致。"""
    memory, redis_store = stores
    base = time.time()

    results = []
    for store in (memory, redis_store):
        store.create(_pending("old", "s1", created_at=base - 100, lifetime=10))
        store.create(_pending("alive", "s1", created_at=base, lifetime=60))
        count = store.purge_expired(now=base)
        results.append((count, store.get("old").status, store.get("alive").status))

    assert results[0] == results[1] == (1, "expired", "pending")

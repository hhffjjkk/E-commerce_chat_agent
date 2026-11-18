"""Guardrails 在 Redis pending 后端下的端到端行为：锁、降级、跨实例可见性。"""

import os
import sys
import time
from contextlib import contextmanager
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
from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.harness import guardrails as guardrails_module  # noqa: E402
from agent_system.harness.guardrails import Guardrails  # noqa: E402
from agent_system.harness.pending_store import PendingAction, reset_pending_store  # noqa: E402
from agent_system.harness.redis_pending_store import RedisPendingStore  # noqa: E402


class FakePipeline:
    """简化 pipeline：排队后按序执行（本文件不需要 WATCH 冲突）。"""

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
    """pending store 所需命令的 Redis 替身（多个 Store 实例共享同一对象）。"""

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
    monkeypatch.setenv("PENDING_STORE_BACKEND", "redis")
    yield
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()


@pytest.fixture
def fake(monkeypatch):
    """配置 Redis 环境并返回共享的假客户端。"""
    client = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: client)
    get_settings.cache_clear()
    reset_redis_manager()
    reset_pending_store()
    return client


@pytest.fixture
def events(monkeypatch):
    """收集 confirmation_* 事件。"""
    captured = []

    def fake_log_event(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(guardrails_module, "log_event", fake_log_event)
    return captured


@pytest.fixture
def write_impl(monkeypatch):
    """替换写操作实现，避免触碰真实数据。"""
    calls = []

    def fake_apply(order_id, reason):
        calls.append(("apply_refund", order_id, reason))
        return {"found": True, "success": True, "message": "退款已提交"}

    monkeypatch.setattr(tools, "_apply_refund_impl", fake_apply)
    monkeypatch.setattr(
        tools,
        "_cancel_order_impl",
        lambda order_id: {"found": True, "success": True, "message": "订单已取消"},
    )
    return calls


def _pending(action_id="a1", session_id="s1", *, expires_in=60.0):
    # pending 时间戳是墙钟
    now = time.time()
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        tool_name="apply_refund",
        args={"order_id": "20261103001", "reason": "尺寸不合适"},
        summary="为订单 20261103001 申请退款，原因：尺寸不合适",
        created_at=now,
        expires_at=now + expires_in,
    )


def test_lock_contention_maps_to_mismatch(fake, write_impl):
    """两个 worker 同时 confirm：未获得锁的一方得到 confirmation_mismatch。"""
    store = RedisPendingStore(ttl_seconds=3600)
    store.create(_pending("a1", "s1"))

    calls = {"n": 0}

    @contextmanager
    def lock_factory(action_id):
        calls["n"] += 1
        # 第一次允许执行，第二次模拟锁被其他实例持有
        yield calls["n"] == 1

    guard = Guardrails(ttl_seconds=300, store=store, lock_factory=lock_factory)

    first = guard.confirm("a1", "s1")
    assert first["success"] is True

    with pytest.raises(AgentError) as exc_info:
        guard.confirm("a1", "s1")
    assert exc_info.value.code == "confirmation_mismatch"
    assert len(write_impl) == 1


def test_confirm_redis_unavailable_maps_to_mismatch(fake, write_impl, events):
    """confirm 时 Redis 不可达：409 + confirmation_failed(redis_unavailable)。"""
    store = RedisPendingStore(ttl_seconds=3600)
    store.create(_pending("a1", "s1"))
    fake.error = ConnectionError("boom")

    guard = Guardrails(ttl_seconds=300, store=store)

    with pytest.raises(AgentError) as exc_info:
        guard.confirm("a1", "s1")

    assert exc_info.value.code == "confirmation_mismatch"
    assert write_impl == []
    failed = [e for e in events if e["event"] == "confirmation_failed"]
    assert failed and failed[-1]["error_code"] == "redis_unavailable"


def test_intercept_write_redis_unavailable_maps_to_confirmation_required(
    fake, write_impl
):
    """intercept_write 时 Redis 不可达：confirmation_required + reason。"""
    store = RedisPendingStore(ttl_seconds=3600)
    fake.error = ConnectionError("boom")
    guard = Guardrails(ttl_seconds=300, store=store)

    with pytest.raises(AgentError) as exc_info:
        guard.intercept_write("apply_refund", {"order_id": "20261103001", "reason": "x"}, "s1")

    assert exc_info.value.code == "confirmation_required"
    assert exc_info.value.details.get("reason") == "pending_store_unavailable"
    assert write_impl == []


def test_two_store_instances_share_records(fake):
    """多 worker 可见性：实例 A create，实例 B 能读到。"""
    store_a = RedisPendingStore(ttl_seconds=3600)
    store_b = RedisPendingStore(ttl_seconds=3600)

    store_a.create(_pending("a1", "shared"))

    loaded = store_b.get("a1")
    assert loaded is not None
    assert loaded.summary == "为订单 20261103001 申请退款，原因：尺寸不合适"
    assert [p.action_id for p in store_b.get_by_session("shared")] == ["a1"]


def test_expired_pending_confirm_returns_expired(fake, write_impl):
    """业务层已过期的 pending：confirm 返回 confirmation_expired。"""
    store = RedisPendingStore(ttl_seconds=3600)
    store.create(_pending("a1", "s1", expires_in=-1))

    guard = Guardrails(ttl_seconds=300, store=store)

    with pytest.raises(AgentError) as exc_info:
        guard.confirm("a1", "s1")

    assert exc_info.value.code == "confirmation_expired"
    assert write_impl == []


def test_ttl_constraint_exposed_before_create(fake, monkeypatch):
    """Redis TTL 配置过小：构造期即失败（不在 confirm 才暴露）。"""
    monkeypatch.setenv("CONFIRMATION_TTL_SECONDS", "300")
    get_settings.cache_clear()

    with pytest.raises(ValueError) as exc_info:
        RedisPendingStore(ttl_seconds=100)

    assert "必须 ≥ 2 × CONFIRMATION_TTL_SECONDS" in str(exc_info.value)

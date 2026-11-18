"""pending 墙钟语义、过期容差与跨实例清理测试。"""

import inspect
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
from agent_system.harness import guardrails as guardrails_module  # noqa: E402
from agent_system.harness import pending_store as pending_store_module  # noqa: E402
from agent_system.harness.guardrails import Guardrails  # noqa: E402
from agent_system.harness.pending_store import (  # noqa: E402
    InMemoryPendingStore,
    PendingAction,
    _is_expired,
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
    """跨实例测试用的共享 Redis 替身。"""

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
    """重置全局配置与单例，并确保容差使用默认值。"""
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


def _pending(*, created_at=1000.0, lifetime=100.0, session_id="s1", action_id="a1"):
    """构造墙钟语义的 pending。"""
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        tool_name="apply_refund",
        args={"order_id": "20261103001", "reason": "test"},
        summary="为订单 20261103001 申请退款，原因：test",
        created_at=created_at,
        expires_at=created_at + lifetime,
    )


def test_intercept_write_uses_wall_clock():
    """intercept_write 生成的时间戳是墙钟，与 time.time() 同步。"""
    guard = Guardrails(ttl_seconds=300, store=InMemoryPendingStore())
    payload = guard.intercept_write(
        "apply_refund", {"order_id": "20261103001", "reason": "test"}, "s1"
    )

    pending = guard.store.get(payload["action_id"])
    assert abs(pending.created_at - time.time()) < 1
    assert pending.expires_at - pending.created_at == pytest.approx(300)


def test_tolerance_boundaries():
    """默认 5 秒容差：expires+3 不过期，expires+6 过期。"""
    pending = _pending(created_at=1000.0, lifetime=100.0)

    assert _is_expired(pending, now=1103.0) is False
    assert _is_expired(pending, now=1106.0) is True


def test_tolerance_is_configurable(monkeypatch):
    """CONFIRMATION_CLOCK_SKEW_SECONDS=0 严格比较；=10 时多容忍 9 秒。"""
    pending = _pending(created_at=1000.0, lifetime=100.0)

    monkeypatch.setenv("CONFIRMATION_CLOCK_SKEW_SECONDS", "0")
    get_settings.cache_clear()
    assert _is_expired(pending, now=1101.0) is True

    monkeypatch.setenv("CONFIRMATION_CLOCK_SKEW_SECONDS", "10")
    get_settings.cache_clear()
    assert _is_expired(pending, now=1109.0) is False
    assert _is_expired(pending, now=1111.0) is True


def test_tolerance_never_exceeds_lifetime():
    """TTL=0 表示立即过期，不因容差被“续命”。"""
    pending = _pending(created_at=1000.0, lifetime=0.0)

    assert _is_expired(pending, now=1000.5) is True


def test_purge_and_confirm_share_same_predicate():
    """purge 与 confirm 引用同一个 _is_expired，防止语义分叉。"""
    assert guardrails_module._is_expired is pending_store_module._is_expired


def _enable_shared_redis(monkeypatch, fake):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    get_settings.cache_clear()
    reset_redis_manager()


def test_cross_instance_active_pending_not_purged(monkeypatch):
    """实例 B 的墙钟比 A 快 400 秒，但 A 的 pending 仍未过期，不应被误清。"""
    fake = FakeRedis()
    _enable_shared_redis(monkeypatch, fake)
    store_a = RedisPendingStore(ttl_seconds=3600)
    store_b = RedisPendingStore(ttl_seconds=3600)

    created = 1_700_000_000.0
    store_a.create(_pending(created_at=created, lifetime=3600))

    assert store_b.purge_expired(now=created + 400) == 0
    assert store_b.get("a1").status == "pending"


def test_cross_instance_really_expired_is_purged(monkeypatch):
    """实例 B 的墙钟推进到超过业务 TTL + 容差，pending 被正确清理。"""
    fake = FakeRedis()
    _enable_shared_redis(monkeypatch, fake)
    store_a = RedisPendingStore(ttl_seconds=3600)
    store_b = RedisPendingStore(ttl_seconds=3600)

    created = 1_700_000_000.0
    store_a.create(_pending(created_at=created, lifetime=10))

    assert store_b.purge_expired(now=created + 20) == 1
    assert store_b.get("a1").status == "expired"


def test_other_monotonic_usages_are_untouched():
    """循环控制 / 重试 / PING 缓存仍使用 monotonic（单进程语义正确）。"""
    from agent_system.cache import redis_manager
    from agent_system.harness import loop_controller, retry_executor

    for module in (loop_controller, retry_executor, redis_manager):
        assert "time.monotonic" in inspect.getsource(module)


def test_session_state_clock_not_changed():
    """会话状态缓存的时间戳本步骤不改，仍使用 time.time（展示语义）。"""
    from agent_system.cache import session_state

    source = inspect.getsource(session_state)
    assert "time.time()" in source
    assert "time.monotonic" not in source

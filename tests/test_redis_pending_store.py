"""RedisPendingStore 协议单测：三组 key、状态机、过期、前缀与降级。"""

import os
import sys
import time
from pathlib import Path

import pytest
import redis as redis_module
from redis.exceptions import WatchError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.cache import RedisUnavailable, reset_redis_manager  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.harness.pending_store import PendingAction, reset_pending_store  # noqa: E402
from agent_system.harness.redis_pending_store import RedisPendingStore  # noqa: E402


class FakePipeline:
    """最小 pipeline 替身：排队执行 + WATCH 冲突注入。"""

    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.reset()

    def watch(self, key):
        self.redis._check()
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
        # 兼容 redis-py 的两种调用：hset(key, mapping=...) 与 hset(key, field, value)
        if isinstance(mapping, str) and field is not None and value is None:
            return self._queue("hset", key, field=mapping, value=field)
        return self._queue("hset", key, mapping=mapping, field=field, value=value)

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

    def hgetall(self, key):
        return self.redis.hgetall(key)

    def execute(self):
        if self.redis.watch_conflicts > 0:
            self.redis.watch_conflicts -= 1
            self.ops = []
            raise WatchError()
        results = [getattr(self.redis, name)(*args, **kwargs) for name, args, kwargs in self.ops]
        self.ops = []
        return results


class FakeRedis:
    """覆盖 pending store 所需命令的 Redis 替身（HASH/SET/ZSET/STRING）。"""

    def __init__(self):
        self.hashes: dict[str, dict] = {}
        self.sets: dict[str, set] = {}
        self.zsets: dict[str, dict] = {}
        self.strings: dict[str, str] = {}
        self.expires: list[tuple[str, int]] = []
        self.error: Exception | None = None
        self.watch_conflicts = 0
        self.pipeline_calls = 0

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
            return len(mapping)
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
        bucket = self.sets.get(key, set())
        before = len(bucket)
        bucket.difference_update(members)
        return before - len(bucket)

    def zadd(self, key, mapping):
        self._check()
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, *members):
        self._check()
        bucket = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if member in bucket:
                del bucket[member]
                removed += 1
        return removed

    def zrangebyscore(self, key, minimum, maximum):
        self._check()
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float(maximum)
        scored = self.zsets.get(key, {})
        selected = sorted(
            (score, member) for member, score in scored.items() if low <= score <= high
        )
        return [member for _, member in selected]

    def expire(self, key, ttl):
        self._check()
        self.expires.append((key, int(ttl)))
        return True

    def delete(self, key):
        self._check()
        existed = any(
            key in bucket for bucket in (self.hashes, self.sets, self.zsets, self.strings)
        )
        for bucket in (self.hashes, self.sets, self.zsets, self.strings):
            bucket.pop(key, None)
        return 1 if existed else 0

    def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        self._check()
        if self.strings.get(key) == token:
            del self.strings[key]
            return 1
        return 0

    def pipeline(self, transaction=True):
        self.pipeline_calls += 1
        return FakePipeline(self)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """每个用例重置单例与配置缓存。"""
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()
    yield
    reset_pending_store()
    reset_redis_manager()
    get_settings.cache_clear()


@pytest.fixture
def fake(monkeypatch):
    """启用假 Redis 并返回它。"""
    client = FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: client)
    get_settings.cache_clear()
    reset_redis_manager()
    return client


@pytest.fixture
def store(fake):
    return RedisPendingStore(ttl_seconds=3600)


def _pending(action_id="a1", session_id="s1", *, created_at=None, expires_in=60.0):
    # pending 时间戳使用墙钟
    now = time.time() if created_at is None else created_at
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        tool_name="apply_refund",
        args={"order_id": "20261103001", "reason": "尺寸不合适"},
        summary=f"为订单 20261103001 申请退款，原因：尺寸不合适",
        created_at=now,
        expires_at=now + expires_in,
        status="pending",
    )


def test_create_writes_three_keys_and_is_idempotent(store, fake):
    """create 原子写入主记录/会话索引/过期索引；重复 create 返回既有记录。"""
    pending = _pending("a1", "s1")
    created = store.create(pending)

    assert created is pending
    assert fake.hashes["pending:a1"]["status"] == "pending"
    assert fake.hashes["pending:a1"]["session_id"] == "s1"
    assert "a1" in fake.sets["pending:session:s1"]
    assert fake.zsets["pending:expiry"]["a1"] == pytest.approx(pending.expires_at)
    assert ("pending:a1", 3600) in fake.expires
    assert fake.pipeline_calls >= 1

    # 重复 create：幂等返回既有记录，不新增
    again = store.create(_pending("a1", "s1"))
    assert again.action_id == "a1"
    assert again.summary == pending.summary
    assert len(fake.sets["pending:session:s1"]) == 1


def test_get_round_trip_and_missing(store, fake):
    """get 还原字段与 dict 类型；不存在返回 None。"""
    pending = _pending("a1", "s1")
    store.create(pending)

    loaded = store.get("a1")
    assert loaded.action_id == "a1"
    assert loaded.args == pending.args
    assert isinstance(loaded.args, dict)
    assert loaded.result is None
    assert loaded.expires_at == pytest.approx(pending.expires_at)

    assert store.get("missing") is None


def test_get_by_session_sorted_and_filters_dangling(store, fake):
    """按 created_at 升序；索引里的悬空 action_id 被过滤。"""
    base = time.time()
    store.create(_pending("a2", "s1", created_at=base + 2))
    store.create(_pending("a1", "s1", created_at=base + 1))
    fake.sets["pending:session:s1"].add("dangling")

    items = store.get_by_session("s1")
    assert [p.action_id for p in items] == ["a1", "a2"]
    assert store.get_by_session("empty") == []


def test_mark_transitions_and_idempotency(store, fake):
    """状态迁移、终态索引清理与幂等/非法迁移文案。"""
    store.create(_pending("a1", "s1"))

    confirmed = store.mark("a1", "confirmed")
    assert confirmed.status == "confirmed"
    assert "a1" in fake.sets["pending:session:s1"]
    assert "a1" in fake.zsets["pending:expiry"]

    executed = store.mark("a1", "executed", result={"success": True})
    assert executed.status == "executed"
    assert executed.result == {"success": True}
    assert fake.hashes["pending:a1"]["status"] == "executed"
    assert "a1" not in fake.sets["pending:session:s1"]
    assert "a1" not in fake.zsets["pending:expiry"]

    # 同状态重复 mark 幂等
    assert store.mark("a1", "executed").status == "executed"

    with pytest.raises(ValueError) as exc_info:
        store.mark("a1", "pending")
    assert "非法状态迁移: executed -> pending" in str(exc_info.value)

    with pytest.raises(ValueError) as exc_info:
        store.mark("a1", "unknown")
    assert str(exc_info.value) == "非法 pending 状态: unknown"

    with pytest.raises(KeyError) as exc_info:
        store.mark("missing", "confirmed")
    assert str(exc_info.value) == "'pending action 不存在: missing'"


def test_mark_retries_on_watch_conflict(store, fake):
    """WATCH 冲突两次后第三次成功。"""
    store.create(_pending("a1", "s1"))
    fake.watch_conflicts = 2

    updated = store.mark("a1", "confirmed")
    assert updated.status == "confirmed"
    assert fake.watch_conflicts == 0


def test_purge_expired_counts_only_pending(store, fake):
    """过期清理只统计 pending → expired；已终态不计入。"""
    base = time.time()
    store.create(_pending("expired", "s1", created_at=base - 10, expires_in=1))
    store.create(_pending("alive", "s1", created_at=base, expires_in=60))
    store.create(_pending("terminal", "s1", created_at=base - 10, expires_in=1))
    store.mark("terminal", "rejected")

    count = store.purge_expired(now=base + 5)

    assert count == 1
    assert store.get("expired").status == "expired"
    assert store.get("alive").status == "pending"
    assert store.get("terminal").status == "rejected"
    # 已转换的 pending 已从过期索引移除，重复清理不再计数
    assert store.purge_expired(now=base + 6) == 0


def test_clear_session_removes_records_and_indexes(store, fake):
    """清空会话：主记录、会话索引、过期索引同步清理，返回条数。"""
    store.create(_pending("a1", "s1"))
    store.create(_pending("a2", "s1"))
    store.create(_pending("a3", "s2"))

    assert store.clear_session("s1") == 2
    assert "a1" not in fake.hashes and "a2" not in fake.hashes
    assert fake.sets.get("pending:session:s1", set()) == set()
    assert "a1" not in fake.zsets.get("pending:expiry", {})
    assert store.get_by_session("s2")[0].action_id == "a3"
    assert store.clear_session("none") == 0


def test_prefix_applies_to_all_keys(fake):
    """多租户前缀作用于主记录、会话索引、过期索引。"""
    prefixed = RedisPendingStore(ttl_seconds=3600, prefix="tenantA")
    prefixed.create(_pending("a1", "s1"))

    assert "tenantA:pending:a1" in fake.hashes
    assert "a1" in fake.sets["tenantA:pending:session:s1"]
    assert "a1" in fake.zsets["tenantA:pending:expiry"]


def test_ttl_constraint_raises_value_error(fake, monkeypatch):
    """Redis TTL 小于 2 倍业务 TTL：构造期中文报错。"""
    monkeypatch.setenv("CONFIRMATION_TTL_SECONDS", "300")
    get_settings.cache_clear()

    with pytest.raises(ValueError) as exc_info:
        RedisPendingStore(ttl_seconds=100)

    assert "PENDING_REDIS_TTL_SECONDS 必须 ≥ 2 × CONFIRMATION_TTL_SECONDS" in str(
        exc_info.value
    )


def test_redis_failure_maps_to_unavailable(store, fake):
    """Redis 故障：方法抛 RedisUnavailable，不泄露底层异常类型。"""
    fake.error = ConnectionError("boom")

    with pytest.raises(RedisUnavailable) as exc_info:
        store.get("a1")
    assert str(exc_info.value) == "connect_error"

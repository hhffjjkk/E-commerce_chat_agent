"""PendingActionStore 单测：CRUD、状态迁移、TTL 清理、并发与单例。"""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness.pending_store import (  # noqa: E402
    InMemoryPendingStore,
    PendingAction,
    get_pending_store,
    reset_pending_store,
)


def _pending(action_id="a1", session_id="s1", *, expires_in=60.0) -> PendingAction:
    """构造一条 pending 记录。"""
    # pending 时间戳使用墙钟；测试也使用 time.time() 保持语义一致
    now = time.time()
    return PendingAction(
        action_id=action_id,
        session_id=session_id,
        tool_name="apply_refund",
        args={"order_id": "20261103001", "reason": "test"},
        summary="为订单 20261103001 申请退款，原因：test",
        created_at=now,
        expires_at=now + expires_in,
    )


def test_create_get_and_list_by_session():
    """基础写入、按 ID 查询与按会话列表。"""
    store = InMemoryPendingStore()
    first = _pending("a1", "s1")
    second = _pending("a2", "s1")
    third = _pending("a3", "s2")
    for item in (first, second, third):
        store.create(item)

    assert store.get("a1") is first
    assert store.get("missing") is None
    assert [p.action_id for p in store.get_by_session("s1")] == ["a1", "a2"]
    assert [p.action_id for p in store.get_by_session("s2")] == ["a3"]


def test_mark_is_idempotent_and_rejects_illegal_transitions():
    """同状态重复 mark 幂等；非法迁移抛 ValueError。"""
    store = InMemoryPendingStore()
    store.create(_pending())

    store.mark("a1", "confirmed")
    assert store.mark("a1", "confirmed").status == "confirmed"

    with pytest.raises(ValueError):
        store.mark("a1", "expired")
    with pytest.raises(ValueError):
        store.mark("a1", "unknown")

    store.mark("a1", "executed", result={"success": True})
    assert store.get("a1").result == {"success": True}
    with pytest.raises(ValueError):
        store.mark("a1", "pending")


def test_purge_expired_only_touches_pending():
    """过期清理只处理 pending，返回条数。"""
    store = InMemoryPendingStore()
    expired = _pending("a1", expires_in=0)
    alive = _pending("a2", expires_in=60)
    store.create(expired)
    store.create(alive)

    # 默认容差 5 秒：推进 6 秒确保越过容差窗口
    assert store.purge_expired(now=time.time() + 6) == 1
    assert store.get("a1").status == "expired"
    assert store.get("a2").status == "pending"


def test_clear_session():
    """按会话清空并返回删除条数。"""
    store = InMemoryPendingStore()
    store.create(_pending("a1", "s1"))
    store.create(_pending("a2", "s1"))
    store.create(_pending("a3", "s2"))

    assert store.clear_session("s1") == 2
    assert store.get_by_session("s1") == []
    assert len(store.get_by_session("s2")) == 1


def test_concurrent_create_does_not_lose_data():
    """10 线程并发 create 不丢数据。"""
    store = InMemoryPendingStore()

    def worker(index):
        store.create(_pending(f"a{index}", "concurrent"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.get_by_session("concurrent")) == 10


def test_reset_pending_store():
    """reset 后单例重新创建且为空。"""
    reset_pending_store()
    first = get_pending_store()
    first.create(_pending())
    assert get_pending_store() is first

    reset_pending_store()
    second = get_pending_store()
    assert second is not first
    assert second.get("a1") is None

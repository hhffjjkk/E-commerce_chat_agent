# agent_system/harness/pending_store.py
# 写操作待确认记录存储：接口 + 进程内实现（阶段五将替换为 Redis 实现）。
# 状态机：pending -> confirmed -> executed；pending -> rejected；pending -> expired。

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

# 允许的状态迁移；同状态重复 mark 视为幂等
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "rejected", "expired"},
    "confirmed": {"executed", "rejected"},
    "executed": {"executed"},
    "rejected": {"rejected"},
    "expired": {"expired"},
}

VALID_STATUSES = {"pending", "confirmed", "rejected", "expired", "executed"}


@dataclass
class PendingAction:
    """一条待确认的写操作记录。

    字段说明：
        action_id: 短 UUID，返回给 LLM/用户用于确认。
        session_id: 归属会话；跨会话确认会被拒绝。
        tool_name: apply_refund / cancel_order。
        args: 原始工具参数（服务端持有，不完整回显给用户）。
        summary: 服务端生成的中文摘要（防止 LLM 伪造）。
        created_at: time.monotonic() 创建时刻。
        expires_at: 过期时刻（created_at + ttl）。
        status: pending / confirmed / rejected / expired / executed。
        result: 执行结果（executed 时填充，用于幂等返回）。
    """

    action_id: str
    session_id: str
    tool_name: str
    args: dict
    summary: str
    created_at: float
    expires_at: float
    status: str = "pending"
    result: dict | None = None


class PendingActionStore(Protocol):
    """待确认记录存储接口；阶段五的 Redis 实现只需满足这些方法。"""

    def create(self, pending: PendingAction) -> PendingAction:
        """写入一条新记录并返回它。"""
        ...

    def get(self, action_id: str) -> PendingAction | None:
        """按 action_id 查询；不存在返回 None。"""
        ...

    def get_by_session(self, session_id: str) -> list[PendingAction]:
        """返回某会话下全部记录（按创建时间升序）。"""
        ...

    def mark(
        self,
        action_id: str,
        status: str,
        *,
        result: dict | None = None,
    ) -> PendingAction:
        """更新状态；非法迁移抛 ValueError，同状态重复调用幂等。"""
        ...

    def purge_expired(self, now: float | None = None) -> int:
        """把已过期的 pending 标记为 expired，返回清理条数。"""
        ...

    def clear_session(self, session_id: str) -> int:
        """删除某会话全部记录，返回删除条数。"""
        ...


class InMemoryPendingStore:
    """进程内实现：dict + RLock。

    为什么不用后台线程做清理：
        1. 后台定时器难以测试（依赖真实时间流逝），也容易在 fork/多 worker 下重复执行；
        2. 过期判断是幂等的，调用方在 intercept/confirm 前顺手清理即可；
        3. 本步骤只要求进程内可用，多实例一致性留给阶段五 Redis 实现。
    """

    def __init__(self) -> None:
        self._items: dict[str, PendingAction] = {}
        self._lock = threading.RLock()

    def create(self, pending: PendingAction) -> PendingAction:
        """写入记录；同一 action_id 重复创建会抛 ValueError。"""
        with self._lock:
            if pending.action_id in self._items:
                raise ValueError(f"pending action 已存在: {pending.action_id}")
            self._items[pending.action_id] = pending
            return pending

    def get(self, action_id: str) -> PendingAction | None:
        """按 ID 查询记录（返回内部对象，调用方不要直接修改状态）。"""
        with self._lock:
            return self._items.get(action_id)

    def get_by_session(self, session_id: str) -> list[PendingAction]:
        """返回指定会话的记录，按创建时间升序。"""
        with self._lock:
            items = [p for p in self._items.values() if p.session_id == session_id]
        return sorted(items, key=lambda p: p.created_at)

    def mark(
        self,
        action_id: str,
        status: str,
        *,
        result: dict | None = None,
    ) -> PendingAction:
        """更新状态并可选写入执行结果。

        参数:
            action_id: 目标记录。
            status: 目标状态。
            result: 执行结果（仅 executed 时使用）。

        返回:
            更新后的记录。

        异常:
            KeyError: 记录不存在。
            ValueError: 目标状态非法或迁移不被允许。
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 pending 状态: {status}")
        with self._lock:
            pending = self._items.get(action_id)
            if pending is None:
                raise KeyError(f"pending action 不存在: {action_id}")

            # 同状态重复调用幂等：直接返回，允许 result 补齐
            if pending.status == status:
                if result is not None:
                    pending.result = result
                return pending

            allowed = _ALLOWED_TRANSITIONS.get(pending.status, set())
            if status not in allowed:
                raise ValueError(
                    f"非法状态迁移: {pending.status} -> {status} (action_id={action_id})"
                )

            pending.status = status
            if result is not None:
                pending.result = result
            return pending

    def purge_expired(self, now: float | None = None) -> int:
        """把过期 pending 标记为 expired，返回处理条数。

        说明:
            只处理 pending 状态；已确认/已执行记录即使超时也不改写，避免覆盖真实结果。
        """
        current = time.monotonic() if now is None else now
        count = 0
        with self._lock:
            for pending in self._items.values():
                if pending.status == "pending" and pending.expires_at <= current:
                    pending.status = "expired"
                    count += 1
        return count

    def clear_session(self, session_id: str) -> int:
        """删除某会话全部记录，返回删除条数。"""
        with self._lock:
            ids = [
                action_id
                for action_id, pending in self._items.items()
                if pending.session_id == session_id
            ]
            for action_id in ids:
                del self._items[action_id]
            return len(ids)


def new_action_id() -> str:
    """生成 16 位 action_id（uuid4 hex 截断），足够短且碰撞概率可忽略。"""
    return uuid.uuid4().hex[:16]


_store: PendingActionStore | None = None


def get_pending_store() -> PendingActionStore:
    """返回全局 PendingActionStore 单例（默认进程内实现）。"""
    global _store
    if _store is None:
        _store = InMemoryPendingStore()
    return _store


def reset_pending_store() -> None:
    """重置全局 Store（仅供测试隔离；生产代码不得调用）。"""
    global _store
    _store = None

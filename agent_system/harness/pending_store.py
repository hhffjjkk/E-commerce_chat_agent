# agent_system/harness/pending_store.py
# 写操作待确认记录存储：接口 + 进程内实现（阶段五将替换为 Redis 实现）。
# 状态机：pending -> confirmed -> executed；pending -> rejected；pending -> expired。

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from agent_system.config import get_settings
from agent_system.obs.events import log_event

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
        created_at: 创建时刻，墙钟时间（UTC epoch 秒）。
            为什么不用 time.monotonic()：pending 会在多 worker / 多主机间共享，
            monotonic 的起点随进程启动时刻变化，跨进程不可比较；
            墙钟配合 NTP 同步才能让不同实例对“是否过期”得出同一结论。
        expires_at: 过期时刻（created_at + ttl），同为墙钟时间。
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


def _is_expired(pending: PendingAction, *, now: float | None = None) -> bool:
    """判断 pending 是否已过期（purge / confirm / reject 共用同一口径）。

    参数:
        pending: 待判断记录。
        now: 用于比较的当前时间；None 时使用 time.time()（墙钟）。

    返回:
        True 表示已过期。

    容差说明:
        多实例墙钟通常由 NTP 同步（毫秒级偏差），但 NTP 调整、虚拟机暂停恢复
        可能造成数秒跳变。若严格按 expires_at 比较，墙钟稍快的实例会误清理
        另一实例刚创建、用户正在确认的 pending。因此判定为：
            now >= expires_at + CONFIRMATION_CLOCK_SKEW_SECONDS
        （边界取等号：与旧实现的 `expires_at <= now` 一致，避免系统时钟
        分辨率较粗时 TTL=0 的“立即过期”永远等不到严格大于。）
        容差**只放宽“已过期”**，不会让未过期记录提前失效；
        方向是安全的：宁可多存活几秒，也不误清理。

    为什么容差在判断侧而不是存储侧:
        expires_at 是业务承诺的过期时刻，不应因实现容差而改变；
        容差属于“读取时如何解释”，放在判断侧才能让所有调用方共享同一语义。

    容差上限:
        容差不超过该记录本身的存活时长（expires_at - created_at）。
        否则 TTL=0（立即过期，测试/应急场景）会被容差“续命”5 秒，
        与“立即过期”的业务语义冲突。
    """
    current = time.time() if now is None else now
    lifetime = max(0.0, pending.expires_at - pending.created_at)
    tolerance = min(get_settings().confirmation_clock_skew_seconds, lifetime)
    return current >= pending.expires_at + tolerance


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
        """写入记录；同一 action_id 已存在时返回既有记录（幂等）。

        为什么改为幂等（与 RedisPendingStore 对齐）:
            两阶段确认的重试路径（网络重试、Guardrails 的保护性重放）可能重复
            create；若内存后端抛错、Redis 后端返回既有记录，同一业务在切换后端时
            会以不同方式失败，破坏阶段六“后端切换结果一致”的评测前提。
        """
        with self._lock:
            existing = self._items.get(pending.action_id)
            if existing is not None:
                return existing
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

        参数:
            now: 用于比较的当前时间；None 时使用 time.time()（墙钟）。

        说明:
            只处理 pending 状态；已确认/已执行记录即使超时也不改写，避免覆盖真实结果。
            过期判断统一走 `_is_expired`（含时钟容差），与 confirm/reject 保持同一口径。
        """
        current = time.time() if now is None else now
        count = 0
        with self._lock:
            for pending in self._items.values():
                if pending.status == "pending" and _is_expired(pending, now=current):
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
_store_lock = threading.Lock()


def pending_backend_name() -> str:
    """返回当前已选后端名（memory / redis），不触发后端选择。

    说明:
        Guardrails 用它决定 confirm 是否需要分布式锁；未选择时按 memory 处理，
        因为此时还没有任何 Redis pending 记录。
    """
    if isinstance(_store, InMemoryPendingStore):
        return "memory"
    if _store is None:
        return "memory"
    return getattr(_store, "backend", "memory")


def _build_redis_store(settings):
    """延迟导入并构造 RedisPendingStore（避免模块循环）。

    参数:
        settings: 当前配置快照。

    异常:
        RedisUnavailable: Redis 已配置但不可达（auto/redis 模式下显式失败）。
    """
    from agent_system.harness.redis_pending_store import RedisPendingStore

    return RedisPendingStore(
        ttl_seconds=settings.pending_redis_ttl_seconds,
        prefix=settings.redis_key_prefix,
    )


def get_pending_store() -> PendingActionStore:
    """返回全局 PendingActionStore 单例，并按配置选择后端。

    选择逻辑:
        - auto（默认）：
            REDIS_URL 为空 → InMemoryPendingStore（单机/测试）；
            REDIS_URL 已配置 → RedisPendingStore；不可达时抛 RedisUnavailable，
            **不自动回退内存**——pending 是写操作的两阶段状态，分叉会导致
            同一笔退款在不同实例上可能执行两次，宁可显式失败让运维介入。
        - memory：强制进程内实现（单机开发/测试）。
        - redis：强制 Redis 实现；不可达时抛 RedisUnavailable。

    返回:
        已缓存的单例；首次调用时完成选择并记录 pending_store_selected。
    """
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is not None:
            return _store

        settings = get_settings()
        backend = settings.pending_store_backend

        if backend == "memory":
            _store = InMemoryPendingStore()
            log_event("pending_store_selected", backend="memory", reason="forced")
            return _store

        if backend == "redis":
            # 构造期会做连通性验证，不可达直接抛 RedisUnavailable
            _store = _build_redis_store(settings)
            log_event("pending_store_selected", backend="redis", reason="forced")
            return _store

        # auto：仅当显式配置 REDIS_URL 时才使用 Redis，避免“本地没装 Redis 就起不来”
        if not os.getenv("REDIS_URL"):
            _store = InMemoryPendingStore()
            log_event("pending_store_selected", backend="memory", reason="auto_default")
            return _store

        _store = _build_redis_store(settings)
        log_event("pending_store_selected", backend="redis", reason="config")
        return _store


def reset_pending_store() -> None:
    """重置全局 Store（仅供测试隔离；生产代码不得调用）。

    说明:
        只丢弃引用，不主动关闭 Redis 连接池：连接池归 RedisManager 统一管理。
    """
    global _store
    with _store_lock:
        _store = None

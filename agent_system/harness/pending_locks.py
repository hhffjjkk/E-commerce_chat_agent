# agent_system/harness/pending_locks.py
# pending 写操作的轻量分布式锁：SET NX EX + Lua 校验 value 后释放。
#
# 为什么不用 redis-py 的 Lock 类：
#   Lock 的 blocking_timeout 边界、thread_local 语义在多实例 + 短 TTL 场景下不易审计；
#   手写 SET NX EX + Lua 释放更直接，也便于用 mock 覆盖“获取失败/释放不匹配”分支。

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from agent_system.cache.errors import RedisUnavailable
from agent_system.cache.redis_manager import get_redis_manager
from agent_system.config import get_settings
from agent_system.obs.events import log_event
from agent_system.obs.logging_config import get_logger

# 只有 value 匹配（说明锁仍属于自己）才删除，避免误删其他实例持有的锁
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


def _hash_id(value: str) -> str:
    """对 action_id 做不可逆哈希前缀，日志中不出现原文。"""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _classify(exc: Exception) -> str:
    """把 Redis 异常归类为简短原因码。"""
    if isinstance(exc, RedisUnavailable):
        return str(exc) or "unavailable"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return "connect_error"
    return type(exc).__name__


def _lock_key(action_id: str, prefix: str) -> str:
    """构造锁 key；prefix 为空时保持无前缀形态。"""
    if prefix:
        return f"{prefix}:pending:lock:{action_id}"
    return f"pending:lock:{action_id}"


@contextmanager
def acquire_pending_lock(
    action_id: str,
    *,
    ttl_seconds: int | None = None,
    prefix: str | None = None,
) -> Iterator[bool]:
    """尝试获取某 action_id 的分布式锁。

    参数:
        action_id: 待确认动作 ID。
        ttl_seconds: 锁过期时间；None 时读取 PENDING_LOCK_TTL_SECONDS（默认 10）。
        prefix: 多租户 key 前缀；None 时读取 REDIS_KEY_PREFIX。

    返回:
        上下文管理器；成功获取时 yield True，离开上下文时安全释放；
        锁已被其他实例持有时 yield False（不抛异常），由调用方决定映射为 409。

    异常:
        RedisUnavailable: Redis 不可达；调用方（Guardrails.confirm）会映射为
            confirmation_mismatch，宁可让用户重试也不冒险双写。

    说明:
        锁 TTL 必须大于 confirm 中执行写工具的最坏耗时（JSON 文件读改写 < 100ms），
        10 秒是安全余量；同时远小于 CONFIRMATION_TTL_SECONDS，避免锁长期占用。
    """
    settings = get_settings()
    ttl = settings.pending_lock_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
    key_prefix = settings.redis_key_prefix if prefix is None else prefix
    key = _lock_key(action_id, key_prefix)
    token = uuid.uuid4().hex

    try:
        client = get_redis_manager().get_client()
        if client is None:
            raise RedisUnavailable("not_configured")
        acquired = bool(client.set(key, token, nx=True, ex=ttl))
    except RedisUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一映射为基础设施异常
        reason = _classify(exc)
        log_event(
            "pending_redis_failed", session_id=None, op="lock_acquire", reason=reason
        )
        raise RedisUnavailable(reason) from exc

    if not acquired:
        # 另一个实例正在 confirm 同一 action：调用方应返回 confirmation_mismatch
        yield False
        return

    # 成功事件默认只到 DEBUG，避免高频锁日志淹没 info 级别输出
    try:
        get_logger().debug(
            "pending_lock_acquired",
            extra={
                "event": "pending_lock_acquired",
                "action_id_hash": _hash_id(action_id),
                "op": "confirm",
            },
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响锁语义
        pass

    try:
        yield True
    finally:
        try:
            released = client.eval(_RELEASE_SCRIPT, 1, key, token)
        except Exception:  # noqa: BLE001 - 释放失败交给 TTL 兜底
            released = 0
        if not released:
            # value 不匹配：锁已过期并被别人持有，不能强行删除
            log_event(
                "pending_lock_release_mismatch",
                action_id_hash=_hash_id(action_id),
                reason="value_mismatch_or_expired",
            )

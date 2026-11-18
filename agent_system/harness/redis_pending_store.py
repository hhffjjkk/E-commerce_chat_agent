# agent_system/harness/redis_pending_store.py
# PendingActionStore 的 Redis 实现：支持多实例共享、状态机与过期索引。
#
# Key 设计（三组 key 各司其职）：
#   主记录   {prefix}:pending:{action_id}          HASH，回答“这个 action 的详情”
#   会话索引 {prefix}:pending:session:{session_id}  SET，回答“这个会话有哪些 action”
#   过期索引 {prefix}:pending:expiry               ZSET，回答“全局哪些 action 已过期”
# 三者必须在同一逻辑操作内维护，否则会出现“主记录还在但索引丢了”的不一致。

from __future__ import annotations

import json
import time
from typing import Any

from redis.exceptions import WatchError

from agent_system.cache.errors import RedisUnavailable
from agent_system.cache.redis_manager import get_redis_manager
from agent_system.config import get_settings
from agent_system.harness.pending_store import (
    VALID_STATUSES,
    PendingAction,
    _ALLOWED_TRANSITIONS,
    _is_expired,
)
from agent_system.obs.events import log_event

_TERMINAL_STATUSES = {"executed", "rejected", "expired"}
_MARK_RETRIES = 3


def _classify(exc: Exception) -> str:
    """把 Redis 异常归类为简短原因码（不含连接串与凭据）。"""
    if isinstance(exc, RedisUnavailable):
        return str(exc) or "unavailable"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return "connect_error"
    return type(exc).__name__


class RedisPendingStore:
    """基于 Redis 的 PendingActionStore 实现。

    与 InMemoryPendingStore 共享同一协议，Guardrails 不感知实现差异。
    多实例部署时，同一 action_id 的 confirm 通过分布式锁串行化；
    mark 使用 WATCH/MULTI 乐观锁处理读-改-写窗口。

    不可用语义:
      - Redis 未配置：`get_pending_store()` 不会选择本后端；
      - Redis 已配置但不可达：所有方法抛 RedisUnavailable，
        由调用方决定降级（Guardrails.confirm 映射为 confirmation_mismatch）。
    """

    backend = "redis"

    def __init__(self, *, ttl_seconds: int = 3600, prefix: str = ""):
        """构造 Redis pending 存储并做启动期校验。

        参数:
            ttl_seconds: 主记录 Redis TTL（秒）。
            prefix: 多租户 key 前缀。

        异常:
            ValueError: Redis TTL 小于 2 倍业务 TTL。
            RedisUnavailable: Redis 已配置但不可达（启动期显式失败）。
        """
        settings = get_settings()
        confirmation_ttl = settings.confirmation_ttl_seconds
        # 两层过期的硬约束：Redis 层必须比业务层晚消失，否则 confirm 时会出现
        # “业务上还没过期，Redis 记录已经没了”的误判。
        if ttl_seconds < 2 * confirmation_ttl:
            raise ValueError(
                "PENDING_REDIS_TTL_SECONDS 必须 ≥ 2 × CONFIRMATION_TTL_SECONDS，"
                "否则 pending 会在业务 TTL 到期前从 Redis 消失"
            )
        self._ttl_seconds = int(ttl_seconds)
        self._prefix = (prefix or "").strip().strip(":")

        # 构造期做一次连通性验证，让 auto/redis 模式在启动/首次选择时显式失败，
        # 而不是等到用户点击“确认”才发现存储不可用。
        client = get_redis_manager().get_client()
        if client is None:
            raise RedisUnavailable("not_configured")

    # ---- Key 构造 -----------------------------------------------------
    def _key(self, action_id: str) -> str:
        if self._prefix:
            return f"{self._prefix}:pending:{action_id}"
        return f"pending:{action_id}"

    def _session_key(self, session_id: str) -> str:
        if self._prefix:
            return f"{self._prefix}:pending:session:{session_id}"
        return f"pending:session:{session_id}"

    @property
    def _expiry_key(self) -> str:
        if self._prefix:
            return f"{self._prefix}:pending:expiry"
        return "pending:expiry"

    # ---- 内部工具 -----------------------------------------------------
    def _run(self, op: str, fn):
        """统一获取客户端并把异常翻译为 RedisUnavailable + 事件。

        参数:
            op: 方法名，用于事件字段。
            fn: 接收 client 的可调用对象。
        """
        try:
            client = get_redis_manager().get_client()
            if client is None:
                raise RedisUnavailable("not_configured")
            return fn(client)
        except RedisUnavailable as exc:
            # 显式传 session_id=None：阻止 log_event 从请求上下文补齐原始会话 ID，
            # 本模块的日志只允许出现操作名与原因码。
            log_event(
                "pending_redis_failed",
                session_id=None,
                op=op,
                reason=str(exc) or "unavailable",
            )
            raise
        except (KeyError, ValueError, RuntimeError):
            # 协议层异常（记录不存在/非法迁移/重试耗尽）不是基础设施故障，
            # 必须原样抛给 Guardrails，绝不能包装成 RedisUnavailable。
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为基础设施异常
            reason = _classify(exc)
            log_event("pending_redis_failed", session_id=None, op=op, reason=reason)
            raise RedisUnavailable(reason) from exc

    @staticmethod
    def _to_mapping(pending: PendingAction) -> dict[str, str]:
        """把 PendingAction 序列化为 HASH 字段（全字符串，便于 redis-cli 查看）。"""
        mapping = {
            "session_id": pending.session_id,
            "tool_name": pending.tool_name,
            "args": json.dumps(pending.args, ensure_ascii=False),
            "summary": pending.summary,
            "created_at": str(float(pending.created_at)),
            "expires_at": str(float(pending.expires_at)),
            "status": pending.status,
        }
        if pending.result is not None:
            mapping["result"] = json.dumps(pending.result, ensure_ascii=False)
        return mapping

    @staticmethod
    def _from_mapping(action_id: str, raw: dict[str, Any]) -> PendingAction:
        """把 HASH 字段还原为 PendingAction。"""
        try:
            args = json.loads(raw.get("args") or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        result_raw = raw.get("result")
        result = None
        if result_raw:
            try:
                result = json.loads(result_raw)
            except (TypeError, json.JSONDecodeError):
                result = None
        return PendingAction(
            action_id=action_id,
            session_id=raw.get("session_id", ""),
            tool_name=raw.get("tool_name", ""),
            args=args if isinstance(args, dict) else {},
            summary=raw.get("summary", ""),
            created_at=float(raw.get("created_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            status=raw.get("status", "pending"),
            result=result,
        )

    # ---- 协议方法 -----------------------------------------------------
    def create(self, pending: PendingAction) -> PendingAction:
        """写入新记录；已存在时返回既有记录（幂等）。

        说明:
            Guardrails 在调用前会按“会话 + 工具 + 参数指纹”去重，
            正常流程不会重复 create；这里的幂等是对网络重试的兜底。
            三组 key 用 pipeline(transaction=True) 原子写入：
            简单的多命令不需要 Lua，Lua 留给需要条件判断的 mark。
        """

        def fn(client):
            key = self._key(pending.action_id)
            if client.exists(key):
                existing = self.get(pending.action_id)
                return existing if existing is not None else pending

            pipe = client.pipeline(transaction=True)
            pipe.hset(key, mapping=self._to_mapping(pending))
            pipe.sadd(self._session_key(pending.session_id), pending.action_id)
            pipe.zadd(self._expiry_key, {pending.action_id: pending.expires_at})
            pipe.expire(key, self._ttl_seconds)
            pipe.execute()
            return pending

        return self._run("create", fn)

    def get(self, action_id: str) -> PendingAction | None:
        """按 action_id 查询；不存在返回 None。"""

        def fn(client):
            raw = client.hgetall(self._key(action_id))
            if not raw:
                return None
            return self._from_mapping(action_id, raw)

        return self._run("get", fn)

    def get_by_session(self, session_id: str) -> list[PendingAction]:
        """返回会话下全部记录，按 created_at 升序。

        说明:
            不过滤 expired（与内存实现一致：过期记录保留到下次 purge）。
            并发清理可能让索引里留下悬空 action_id，这里直接过滤 None。
        """

        def fn(client):
            ids = client.smembers(self._session_key(session_id))
            items = []
            for action_id in ids:
                pending = self.get(action_id)
                if pending is not None:
                    items.append(pending)
            return sorted(items, key=lambda p: p.created_at)

        return self._run("get_by_session", fn)

    def mark(
        self,
        action_id: str,
        status: str,
        *,
        result: dict | None = None,
    ) -> PendingAction:
        """更新状态；非法迁移抛 ValueError，同状态重复调用幂等。

        说明:
            使用 WATCH + MULTI/EXEC 乐观锁：mark 的读-改-写窗口极短且只影响
            单个 action_id，不需要像 confirm 那样串行化整段执行。
            WATCH 冲突重试 3 次，仍失败抛 RuntimeError（调用方无重试逻辑）。
        """
        if status not in VALID_STATUSES:
            raise ValueError(f"非法 pending 状态: {status}")

        def fn(client):
            key = self._key(action_id)
            for _ in range(_MARK_RETRIES):
                pipe = client.pipeline()
                try:
                    pipe.watch(key)
                    raw = pipe.hgetall(key)
                    if not raw:
                        pipe.unwatch()
                        raise KeyError(f"pending action 不存在: {action_id}")

                    current = self._from_mapping(action_id, raw)
                    if current.status == status:
                        # 同状态幂等：允许补齐 result，但不再写状态
                        if result is not None:
                            pipe.multi()
                            pipe.hset(
                                key,
                                "result",
                                json.dumps(result, ensure_ascii=False),
                            )
                            pipe.execute()
                            current.result = result
                            return current
                        pipe.unwatch()
                        return current

                    allowed = _ALLOWED_TRANSITIONS.get(current.status, set())
                    if status not in allowed:
                        pipe.unwatch()
                        raise ValueError(
                            f"非法状态迁移: {current.status} -> {status} "
                            f"(action_id={action_id})"
                        )

                    pipe.multi()
                    pipe.hset(key, "status", status)
                    if result is not None:
                        pipe.hset(
                            key, "result", json.dumps(result, ensure_ascii=False)
                        )
                    if status in _TERMINAL_STATUSES:
                        # 终态从两个索引移除，减少 get_by_session 与 purge 的扫描量；
                        # 主记录保留并交给 TTL 自然回收，供审计与幂等返回使用。
                        pipe.srem(self._session_key(current.session_id), action_id)
                        pipe.zrem(self._expiry_key, action_id)
                    pipe.execute()

                    current.status = status
                    if result is not None:
                        current.result = result
                    return current
                except WatchError:
                    # 乐观锁冲突：重新读取后重试
                    continue
                finally:
                    try:
                        pipe.reset()
                    except Exception:  # noqa: BLE001
                        pass
            raise RuntimeError(f"pending 状态更新冲突，重试耗尽: {action_id}")

        return self._run("mark", fn)

    def purge_expired(self, now: float | None = None) -> int:
        """把已过期的 pending 标记为 expired，返回清理条数。

        参数:
            now: 用于比较的当前时间；None 时使用 time.time()（墙钟）。

        说明:
            不直接 DEL：审计需要保留终态一段时间，主记录靠 Redis TTL 回收；
            只统计从 pending 成功迁移的数量（与内存实现一致）。
            过期判断统一走 `_is_expired`（含时钟容差）：ZSET 只做粗筛，
            最终是否过期由同一函数决定，避免与 confirm/reject 语义分叉。
        """
        current = time.time() if now is None else now

        def fn(client):
            ids = client.zrangebyscore(self._expiry_key, "-inf", current)
            count = 0
            for action_id in ids:
                pending = self.get(action_id)
                if pending is None:
                    # 主记录已消失（TTL/并发清理）：顺手清掉悬空索引项
                    client.zrem(self._expiry_key, action_id)
                    continue
                if pending.status != "pending":
                    # 已终态：不在过期索引里继续保留
                    client.zrem(self._expiry_key, action_id)
                    continue
                if not _is_expired(pending, now=current):
                    # 仍在容差窗口内：保留在索引里，下一轮再判断
                    continue
                try:
                    self.mark(action_id, "expired")
                    count += 1
                except (KeyError, ValueError):
                    # 并发下状态已被其他实例推进：跳过，不重复计数
                    continue
            return count

        return self._run("purge_expired", fn)

    def clear_session(self, session_id: str) -> int:
        """删除会话下全部记录与索引，返回删除条数。"""

        def fn(client):
            ids = list(client.smembers(self._session_key(session_id)))
            if not ids:
                return 0
            pipe = client.pipeline(transaction=True)
            for action_id in ids:
                pipe.delete(self._key(action_id))
                pipe.zrem(self._expiry_key, action_id)
            pipe.delete(self._session_key(session_id))
            pipe.execute()
            return len(ids)

        return self._run("clear_session", fn)

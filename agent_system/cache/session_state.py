# agent_system/cache/session_state.py
# 会话级元数据缓存：每会话的最近一次路由结果与活跃时间。
#
# 与 RedisSessionStore 的分工：
#   - 历史消息是“可持久内容”，key 为 {prefix}:chat:{sid}，LIST 结构；
#   - 会话状态是“短周期元数据”，key 为 {prefix}:session_state:{sid}，HASH 结构；
#   两者生命周期、数据结构、TTL 都不同，因此必须分 key，不能混在一个对象里。

from __future__ import annotations

import hashlib
import os
import time

from agent_system.cache.errors import RedisUnavailable
from agent_system.cache.redis_manager import get_redis_manager
from agent_system.config import get_settings
from agent_system.obs.events import log_event
from agent_system.obs.logging_config import get_logger


def _hash_session_id(session_id: str) -> str:
    """返回 session_id 的哈希前 8 位，用于日志关联而不暴露原文。

    参数:
        session_id: 原始会话 ID。

    返回:
        8 位十六进制字符串。

    说明:
        排障时需要把同一会话的多次写失败串起来，但 session_id 可能可定位到用户，
        因此只记录不可逆哈希前缀；这与全项目“日志不写用户原文”的约定一致。
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:8]


def _reason_of(exc: Exception) -> str:
    """把异常归类为简短原因码，供 session_state_write_failed 使用。"""
    if isinstance(exc, RedisUnavailable):
        return str(exc) or "unavailable"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return "connect_error"
    return type(exc).__name__


class SessionStateCache:
    """会话级元数据缓存。

    存储每会话最近一次路由结果与活跃时间；不存储历史消息，也不与
    RedisSessionStore 共享 key。

    降级语义:
      - Redis 未配置（RedisManager.is_enabled() 为 False）：所有方法立即 no-op
        （读返回 None），不抛异常、不记失败事件，避免未启用 Redis 的部署被降级噪音淹没；
      - Redis 已配置但不可达：读返回 None；写静默失败并记
        session_state_write_failed；调用方不得因缓存失败而中断请求。

    为什么本层不抛 RedisUnavailable:
        会话状态是**可丢失数据**。读失败等价于未命中，写失败等价于丢弃；
        把基础设施异常抛给 /chat 调用方只会制造“缓存故障拖垮业务”的耦合。
        写失败的观测通过事件完成，而不是通过异常。
    """

    def __init__(self, ttl_seconds: int = 1800, prefix: str = ""):
        """构造会话状态缓存。

        参数:
            ttl_seconds: HASH 的过期时间（秒），必须为正数。
            prefix: 多租户 key 前缀；空串表示不加前缀。

        说明:
            构造时不建立连接、不探活：真实客户端在每次操作时经 RedisManager 惰性获取，
            这样 Redis 恢复后无需重启进程即可自动恢复写入。
        """
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._prefix = (prefix or "").strip().strip(":")
        # 默认不记录成功事件：会话状态写是每请求一次的高频操作，
        # 正常 INFO 级别记录会造成日志膨胀；排障时用环境变量临时打开。
        raw = os.getenv("SESSION_STATE_LOG_SUCCESS", "").strip().lower()
        self._log_success = raw in {"1", "true", "yes", "on"}

    def _key(self, session_id: str) -> str:
        """构造会话状态的 Redis Key。

        参数:
            session_id: 会话 ID。

        返回:
            `{prefix}:session_state:{session_id}`；prefix 为空时为
            `session_state:{session_id}`。
        """
        if self._prefix:
            return f"{self._prefix}:session_state:{session_id}"
        return f"session_state:{session_id}"

    def _client_or_none(self):
        """返回 (client, configured)。

        client 为 None 时有两种情况：
          - configured=False：Redis 未配置，调用方应完全静默；
          - configured=True：Redis 已配置但不可达，写操作需要记失败事件。
        """
        manager = get_redis_manager()
        if not manager.is_enabled():
            return None, False
        try:
            return manager.get_client(), True
        except RedisUnavailable:
            return None, True

    def get(self, session_id: str) -> dict | None:
        """读取会话状态。

        参数:
            session_id: 会话 ID。

        返回:
            含 last_route_agent / last_route_display_name / last_route_strategy /
            last_route_confidence / last_route_reason / last_route_at /
            last_activity_at 的字典；key 不存在或 Redis 故障时返回 None。

        说明:
            读失败一律视为未命中：调用方无需区分“没有状态”和“读不到状态”。
        """
        client, _ = self._client_or_none()
        if client is None:
            return None
        try:
            raw = client.hgetall(self._key(session_id))
        except Exception:  # noqa: BLE001 - 可丢失数据，读失败按未命中处理
            return None
        if not raw:
            return None

        def _as_int(value):
            """把 Redis 字符串时间戳转成 int；格式异常时返回 None。"""
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        confidence_raw = raw.get("last_route_confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None

        return {
            "last_route_agent": raw.get("last_route_agent"),
            "last_route_display_name": raw.get("last_route_display_name"),
            "last_route_strategy": raw.get("last_route_strategy"),
            "last_route_confidence": confidence,
            "last_route_reason": raw.get("last_route_reason"),
            "last_route_at": _as_int(raw.get("last_route_at")),
            "last_activity_at": _as_int(raw.get("last_activity_at")),
        }

    def set_route(
        self,
        session_id: str,
        *,
        agent: str,
        display_name: str,
        strategy: str,
        confidence: float | None,
        reason: str,
    ) -> None:
        """写入最近一次路由结果并刷新 TTL。

        参数:
            session_id: 会话 ID。
            agent: 子 Agent 标识（如 order / after_sales）。
            display_name: 面向用户的中文名。
            strategy: 路由策略（rule / llm / fallback / pending_override）。
            confidence: 置信度；None 时不写该字段。
            reason: 路由原因码。

        说明:
            只存路由元数据，不存用户消息原文或 LLM 输出；
            任何 Redis 异常都被吞掉并记事件，绝不能影响 /chat 响应。
        """
        client, configured = self._client_or_none()
        if client is None:
            if configured:
                # 已配置但不可达：写视为丢弃，但仍要留下可观测的失败事件
                self._log_write_failed(session_id, "set_route", None)
            return

        now = str(int(time.time()))
        mapping = {
            "last_route_agent": agent,
            "last_route_display_name": display_name,
            "last_route_strategy": strategy,
            "last_route_reason": reason,
            "last_route_at": now,
            "last_activity_at": now,
        }
        if confidence is not None:
            mapping["last_route_confidence"] = str(confidence)

        try:
            key = self._key(session_id)
            client.hset(key, mapping=mapping)
            client.expire(key, self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不能阻塞业务
            self._log_write_failed(session_id, "set_route", exc)
            return

        self._log_updated(session_id, agent, strategy, confidence is not None)

    def touch(self, session_id: str) -> None:
        """仅刷新会话的 last_activity_at 与 TTL。

        参数:
            session_id: 会话 ID。

        说明:
            供后续“活跃度”相关逻辑使用；失败同样静默并记事件。
        """
        client, configured = self._client_or_none()
        if client is None:
            if configured:
                self._log_write_failed(session_id, "touch", None)
            return
        try:
            key = self._key(session_id)
            client.hset(key, mapping={"last_activity_at": str(int(time.time()))})
            client.expire(key, self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            self._log_write_failed(session_id, "touch", exc)

    def delete(self, session_id: str) -> None:
        """删除会话状态；失败静默并记事件。"""
        client, configured = self._client_or_none()
        if client is None:
            if configured:
                self._log_write_failed(session_id, "delete", None)
            return
        try:
            client.delete(self._key(session_id))
        except Exception as exc:  # noqa: BLE001
            self._log_write_failed(session_id, "delete", exc)

    def _log_write_failed(self, session_id: str, op: str, exc: Exception | None) -> None:
        """记录 session_state_write_failed；只写哈希 ID、操作名与原因码。"""
        log_event(
            "session_state_write_failed",
            # 显式传 None：阻止 log_event 从请求上下文自动补齐原始 session_id。
            # 本事件只允许出现哈希 ID，避免日志中出现可定位到用户的会话标识。
            session_id=None,
            session_id_hash=_hash_session_id(session_id),
            op=op,
            reason=_reason_of(exc) if exc is not None else (
                get_redis_manager().last_error or "unavailable"
            ),
        )

    def _log_updated(
        self, session_id: str, agent: str, strategy: str, has_confidence: bool
    ) -> None:
        """可选的成功事件；默认关闭，只有 SESSION_STATE_LOG_SUCCESS 打开时记录。

        说明:
            用 DEBUG 级别而不是 INFO：这是高频旁路事件，排查问题时临时开启即可。
        """
        if not self._log_success:
            return
        try:
            get_logger().debug(
                "session_state_updated",
                extra={
                    "event": "session_state_updated",
                    "session_id_hash": _hash_session_id(session_id),
                    "agent": agent,
                    "strategy": strategy,
                    "has_confidence": has_confidence,
                },
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响业务
            pass


_cache: SessionStateCache | None = None


def get_session_state_cache() -> SessionStateCache:
    """返回全局 SessionStateCache 单例；首次调用时读取前缀与 TTL 配置。"""
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = SessionStateCache(
            ttl_seconds=settings.redis_session_state_ttl_seconds,
            prefix=settings.redis_key_prefix,
        )
    return _cache


def reset_session_state_cache() -> None:
    """重置全局单例；仅供测试隔离使用。"""
    global _cache
    _cache = None

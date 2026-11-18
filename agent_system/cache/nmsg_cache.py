# agent_system/cache/nmsg_cache.py
# 归一化消息缓存：key = 稳定前缀哈希 + 归一化消息哈希，值为最终回复文本。
#
# 命名澄清：这是“归一化消息缓存”，不是 embedding 语义缓存。
#   命中要求两个哈希完全相等，不做相似度匹配；同义句、近义句不会命中。

from __future__ import annotations

import hashlib

from agent_system.cache.errors import RedisUnavailable
from agent_system.cache.nmsg_normalize import is_cacheable, normalize_message
from agent_system.cache.redis_manager import get_redis_manager
from agent_system.config import get_settings
from agent_system.obs.events import log_event


def _hash_message(normalized: str) -> str:
    """归一化消息的短哈希（sha256 前 16 位），key 中不出现原文。"""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


class NmsgCache:
    """基于 Redis 的归一化消息缓存。

    启用条件由调用方（app 层白名单）判断；本类只负责读写与降级。

    降级语义（与 SessionStateCache 一致）：
      - Redis 未配置：get 返回 None、put 返回 False，不产生事件；
      - Redis 已配置但不可达：get 返回 None 并记 cache_get_failed；
        put 返回 False 并记 cache_put_failed；
      - 任何异常都不会传播到 /chat 调用方。

    为什么不缓存完整响应对象:
        缓存只需要让用户看到上次的答案；工具轨迹、路由决策与 usage 对用户不可见，
        缓存它们会带来脱敏与过期不一致的风险。
    """

    def __init__(
        self, *, ttl_seconds: int = 3600, order_ttl_seconds: int = 60, prefix: str = ""
    ):
        """构造缓存；不建立连接，真实客户端在操作时惰性获取。"""
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._order_ttl_seconds = max(1, int(order_ttl_seconds))
        self._prefix = (prefix or "").strip().strip(":")

    def is_enabled(self) -> bool:
        """Redis 是否已配置；未配置时所有操作都是 no-op。"""
        return get_redis_manager().is_enabled()

    def _key(self, stable_prefix_hash: str, message: str) -> str | None:
        """构造缓存 key；消息不可缓存时返回 None。

        参数:
            stable_prefix_hash: ContextAssembler 的稳定前缀哈希。
            message: 用户原始消息。

        返回:
            `{prefix}:nmsg:{stable_prefix_hash}:{msg_hash}`；不可缓存时 None。
        """
        normalized = normalize_message(message)
        if not is_cacheable(normalized):
            return None
        msg_hash = _hash_message(normalized)
        if self._prefix:
            return f"{self._prefix}:nmsg:{stable_prefix_hash}:{msg_hash}"
        return f"nmsg:{stable_prefix_hash}:{msg_hash}"

    def _client_or_none(self):
        """返回 (client, configured)；语义与 SessionStateCache 相同。"""
        manager = get_redis_manager()
        if not manager.is_enabled():
            return None, False
        try:
            return manager.get_client(), True
        except RedisUnavailable:
            return None, True

    def get(self, stable_prefix_hash: str, message: str) -> str | None:
        """查缓存；未命中、不可缓存或 Redis 故障时返回 None。"""
        key = self._key(stable_prefix_hash, message)
        if key is None:
            return None

        client, configured = self._client_or_none()
        if client is None:
            if configured:
                log_event(
                    "cache_get_failed",
                    session_id=None,
                    layer="nmsg",
                    reason=get_redis_manager().last_error or "unavailable",
                )
            return None
        try:
            value = client.get(key)
        except Exception as exc:  # noqa: BLE001 - 缓存读失败视为未命中
            log_event(
                "cache_get_failed",
                session_id=None,
                layer="nmsg",
                reason=_reason_of(exc),
            )
            return None
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    def put(
        self,
        stable_prefix_hash: str,
        message: str,
        reply: str,
        *,
        ttl_seconds: int | None = None,
    ) -> bool:
        """写缓存。

        参数:
            stable_prefix_hash: 稳定前缀哈希。
            message: 用户原始消息。
            reply: 最终 assistant 回复文本。
            ttl_seconds: 覆盖默认 TTL；None 用实例默认值。

        返回:
            True 表示写入成功；False 表示不可缓存或 Redis 失败。

        说明:
            返回值比签名多出来的布尔值用于让 app 层只在真正写入时记 cache_write，
            避免“Redis 失败但仍打成功日志”的假象。
        """
        key = self._key(stable_prefix_hash, message)
        if key is None or not reply:
            return False

        client, configured = self._client_or_none()
        if client is None:
            if configured:
                log_event(
                    "cache_put_failed",
                    session_id=None,
                    layer="nmsg",
                    reason=get_redis_manager().last_error or "unavailable",
                )
            return False

        ttl = self._ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        try:
            client.setex(key, ttl, reply)
        except Exception as exc:  # noqa: BLE001 - 写失败只记录，不影响业务
            log_event(
                "cache_put_failed",
                session_id=None,
                layer="nmsg",
                reason=_reason_of(exc),
            )
            return False
        return True

    def delete(self, stable_prefix_hash: str, message: str) -> None:
        """删除缓存项（当前仅用于测试/运维，不参与业务失效）。"""
        key = self._key(stable_prefix_hash, message)
        if key is None:
            return
        client, _ = self._client_or_none()
        if client is None:
            return
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001 - 删除失败由 TTL 兜底
            pass


def _reason_of(exc: Exception) -> str:
    """把 Redis 异常归类为简短原因码。"""
    if isinstance(exc, RedisUnavailable):
        return str(exc) or "unavailable"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return "connect_error"
    return type(exc).__name__


_cache: NmsgCache | None = None


def get_nmsg_cache() -> NmsgCache:
    """返回全局 NmsgCache 单例；首次调用时读取 TTL 与前缀配置。"""
    global _cache
    if _cache is None:
        settings = get_settings()
        _cache = NmsgCache(
            ttl_seconds=settings.nmsg_ttl_seconds,
            order_ttl_seconds=settings.nmsg_order_ttl_seconds,
            prefix=settings.redis_key_prefix,
        )
    return _cache


def reset_nmsg_cache() -> None:
    """重置全局单例；仅供测试隔离使用。"""
    global _cache
    _cache = None

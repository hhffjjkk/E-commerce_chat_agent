# agent_system/cache/redis_manager.py
# Redis 客户端与健康检查的集中管理。
#
# 为什么集中管理：
#   - 阶段二之前 memory.py 与 runtime/health.py 各自 redis.from_url()，
#     连接参数、超时、是否 decode_responses 全靠各写各的，容易出现“测试能过、
#     线上超时不同”的漂移；
#   - 阶段五后续的会话状态缓存、pending store、语义缓存都要复用同一个连接池，
#     如果各自建客户端，连接数会随使用方数量线性增长。

from __future__ import annotations

import threading
import time

from agent_system.cache.errors import RedisUnavailable
from agent_system.config import get_settings
from agent_system.obs.events import log_event


def _classify_ping_error(exc: Exception) -> str:
    """把 PING 异常归类为简短原因码，供日志与健康检查 detail 使用。

    参数:
        exc: client.ping() 抛出的原始异常。

    返回:
        timeout / connect_error / ping_failed 三者之一。

    说明:
        不把异常消息写进返回值：Redis 的连接串可能含密码，只按异常类型归类，
        既足够排障，又不会泄露凭据。
    """
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "timeout"
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return "connect_error"
    return "ping_failed"


class RedisManager:
    """Redis 客户端与健康检查的集中管理。

    生命周期:
        默认由模块级单例 `get_redis_manager()` 持有；构造时读取一次配置快照。
        测试可 `reset_redis_manager()` 后重新构造。

    职责:
      - 连接池（超时、最大连接数、内建探活间隔）；
      - 惰性 PING 判断可用性，并缓存短时间结果，避免每个请求都真 PING；
      - 不可用时显式抛 RedisUnavailable，不自动回退到其它实现；
      - 记录 redis_ping_failed 事件，便于在日志中看见故障。

    降级边界:
        “是否回退 SQLite/内存”由使用方决定（例如 memory.create_store 依据
        REDIS_FALLBACK_TO_MEMORY），Manager 本身只负责“如实报告不可用”。
    """

    def __init__(self) -> None:
        """按当前配置快照初始化（不建立真实连接，连接在首次 PING 时发生）。"""
        settings = get_settings()
        # None 表示未配置：Manager 处于“未启用”状态，使用方应走 SQLite/内存
        self._url = settings.redis_url
        # 连接池参数：读取一次后固定，运行期变化需要重启进程（与其它配置一致）
        self._socket_timeout = settings.redis_socket_timeout
        self._connect_timeout = settings.redis_connect_timeout
        self._max_connections = settings.redis_max_connections
        self._health_check_interval = settings.redis_health_check_interval
        self._ping_ttl = settings.redis_ping_cache_seconds

        self._client = None  # redis.Redis | None，惰性构造
        self._ping_cache: tuple[float, bool] | None = None  # (单调时钟时间, 结果)
        self._last_error: str | None = None  # 最近一次失败原因码
        # RLock：get_client/ping/reset 可能嵌套调用，可重入锁避免自锁
        self._lock = threading.RLock()

    def is_enabled(self) -> bool:
        """是否配置了 REDIS_URL；未配置时所有方法走“未启用”分支。"""
        return bool(self._url)

    @property
    def last_error(self) -> str | None:
        """最近一次 PING 失败的原因码；用于 /health/ready 的 detail。"""
        return self._last_error

    def get_client(self):
        """返回 Redis 客户端。

        返回:
            未启用时返回 None（使用方据此选择 SQLite/内存实现）；
            已启用且可用时返回连接池客户端。

        异常:
            RedisUnavailable: 已启用但连接失败/超时/PING 失败。
                **不会**自动回退——回退策略由使用方显式决定并记录日志。

        说明:
            首次调用会通过 ping() 触发一次真实探活；后续调用受 PING 缓存保护，
            因此高频请求不会把 Redis 的 QPS 放大一倍。
        """
        if not self.is_enabled():
            return None
        if not self.ping():
            # 显式失败：让 /health/ready 与日志看得见，避免“悄悄写 SQLite”的一致性灾难
            raise RedisUnavailable(self._last_error or "ping_failed")
        with self._lock:
            return self._client

    def ping(self, force: bool = False) -> bool:
        """探活 Redis。

        参数:
            force: True 时忽略缓存，立即发一次真实 PING（健康检查使用）。

        返回:
            True 可用 / False 不可用或未启用；**不抛异常**，便于健康检查分支使用。

        说明:
            - 缓存 TTL 由 REDIS_PING_CACHE_SECONDS 控制（默认 5 秒）：
              会话缓存会在每个请求入口调用 ping，如果每次都真 PING，
              会把 Redis QPS 放大一倍；5 秒窗口既足够及时，也不会让故障探测太迟钝；
            - 失败时记录 redis_ping_failed（只记原因码，不记连接串）。
        """
        if not self.is_enabled():
            return False

        with self._lock:
            client = self._client
            if client is None:
                try:
                    client = self._build_client()
                except RedisUnavailable as exc:
                    # from_url 构造失败（如 redis 包缺失）：同样归入不可用，不抛出
                    self._last_error = str(exc)
                    self._ping_cache = (time.monotonic(), False)
                    log_event("redis_ping_failed", reason=self._last_error)
                    return False
                self._client = client
            cache = self._ping_cache

        now = time.monotonic()
        if not force and cache is not None and (now - cache[0]) < self._ping_ttl:
            return cache[1]

        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001 - 探活需要吞掉所有异常
            self._last_error = _classify_ping_error(exc)
            with self._lock:
                self._ping_cache = (time.monotonic(), False)
            log_event("redis_ping_failed", reason=self._last_error)
            return False

        with self._lock:
            self._ping_cache = (time.monotonic(), True)
            self._last_error = None
        return True

    def reset(self) -> None:
        """关闭旧连接池并清空缓存；仅供测试隔离使用。

        说明:
            生产代码不应调用：运行期重建连接池会让正在使用的客户端失效。
        """
        with self._lock:
            client, self._client = self._client, None
            self._ping_cache = None
            self._last_error = None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响重置语义
                pass

    def _build_client(self):
        """按连接池参数创建客户端；延迟导入 redis，未安装时抛领域异常。

        连接池参数选择依据:
          - socket_timeout / socket_connect_timeout 默认 1 秒：缓存数据可丢失、可重试，
            不应让一次 Redis 抖动把用户请求拖到几秒；
          - max_connections 默认 50：单实例、同步调用的量级足够；
            阶段六压测若出现连接等待，再按并发度调整；
          - health_check_interval 默认 30 秒：redis-py 内建的空闲连接探活，
            避免拿到已被服务端断开的连接才报错。
        """
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - 依赖缺失的极端环境
            raise RedisUnavailable("redis package missing") from exc

        try:
            return redis.from_url(
                self._url,
                decode_responses=True,  # 与既有 RedisSessionStore 的字符串语义保持一致
                socket_timeout=self._socket_timeout,
                socket_connect_timeout=self._connect_timeout,
                max_connections=self._max_connections,
                health_check_interval=self._health_check_interval,
            )
        except Exception as exc:  # noqa: BLE001 - 构造失败统一归为连接不可用
            raise RedisUnavailable("connect_error") from exc


_manager: RedisManager | None = None
_manager_lock = threading.Lock()


def get_redis_manager() -> RedisManager:
    """返回全局 RedisManager 单例；首次调用时按配置构造。"""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = RedisManager()
    return _manager


def reset_redis_manager() -> None:
    """重置全局单例并关闭旧连接池；仅供测试使用。"""
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.reset()

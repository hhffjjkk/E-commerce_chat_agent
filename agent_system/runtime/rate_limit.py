# agent_system/runtime/rate_limit.py
# 基于客户端 IP 的进程内固定窗口限流；不依赖 Redis，也不引入外部限流库。
# 定位：单实例演示足够；多实例部署时应换成 Redis 共享计数（属于后续阶段）。

import math
import threading
import time
from collections import deque

from starlette.datastructures import Headers

from agent_system.config import get_settings
from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError
from agent_system.obs.events import rate_limited
from agent_system.runtime.responses import send_agent_error


def client_ip(scope) -> str:
    """优先取 X-Forwarded-For 首个地址，否则取连接对端地址。

    取首项是因为 XFF 由代理逐跳追加，最左边才是真实客户端；
    两个都拿不到时返回 "unknown"，让这类请求共享一个桶而不是报错。
    """
    headers = Headers(scope=scope)
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"


class RateLimitMiddleware:
    """固定窗口计数：每个 IP 在窗口内最多 N 次请求，超限返回 429。"""

    def __init__(
        self,
        app,
        *,
        enabled: bool | None = None,
        window_seconds: float | None = None,
        max_requests: int | None = None,
        exclude_paths=None,
        cors_allow_origins=None,
    ):
        """保存下游应用与限流参数；所有参数不传时从全局配置读取。

        之所以允许显式传参：测试需要构造确定性的限流场景，而不依赖环境变量。
        """
        settings = get_settings()
        self.app = app
        self.enabled = settings.rate_limit_enabled if enabled is None else bool(enabled)
        self.window_seconds = (
            settings.rate_limit_window_seconds if window_seconds is None else float(window_seconds)
        )
        self.max_requests = (
            settings.rate_limit_max_requests if max_requests is None else int(max_requests)
        )
        self.exclude_paths = frozenset(
            settings.rate_limit_exclude_paths if exclude_paths is None else exclude_paths
        )
        self.cors_allow_origins = frozenset(
            settings.cors_allow_origins if cors_allow_origins is None else cors_allow_origins
        )
        # ip -> 该 IP 在窗口内的请求时间戳队列；deque 便于从左侧淘汰过期项
        self._buckets: dict[str, deque[float]] = {}
        # 纯 ASGI 中间件可能被并发调用，计数必须加锁
        self._lock = threading.Lock()

    def _cors_headers(self, scope) -> dict:
        """为限流拒绝的响应补上 CORS 头。

        CORS 中间件在更内层，命中外层限流时请求不会经过它，
        因此跨域前端需要在这里手动补 header，否则浏览器读不到 429 的 body。
        """
        origin = Headers(scope=scope).get("origin")
        if origin and origin in self.cors_allow_origins:
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    async def __call__(self, scope, receive, send):
        """ASGI 入口：先判断是否需要限流，未超限则放行，超限则返回 429。"""
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        # OPTIONS 预检不计数：否则浏览器预检会把真实请求的配额吃掉
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exclude_paths:
            # 健康检查等探针路径排除在外，避免探针把自己限死
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope)
        now = time.monotonic()  # 单调时钟，不受系统时间回拨影响
        retry_after = None

        with self._lock:
            bucket = self._buckets.setdefault(ip, deque())
            cutoff = now - self.window_seconds
            # 淘汰窗口外的旧记录，实现滑动式清理的固定窗口
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_requests:
                # 计算还需等待多久窗口才会让出一个名额（至少 1 秒）
                retry_after = max(1, math.ceil(self.window_seconds - (now - bucket[0])))
            else:
                bucket.append(now)

        if retry_after is None:
            await self.app(scope, receive, send)
            return

        # 记录限流事件，并返回统一错误结构（429 rate_limited）
        rate_limited(ip, path, self.max_requests, self.window_seconds)
        headers = {"Retry-After": str(retry_after)}
        headers.update(self._cors_headers(scope))
        await send_agent_error(
            scope,
            receive,
            send,
            AgentError(ErrorCode.RATE_LIMITED),
            headers,
        )

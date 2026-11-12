# agent_system/runtime/body_limit.py
# 基于 Content-Length 的请求体大小限制；缺失该头时不做流式截断（见 notes）。
# 这是个「便宜的前置检查」：只读头部，不缓存、不改写 body，超限直接拒绝。

from starlette.datastructures import Headers

from agent_system.config import get_settings
from agent_system.errors.exceptions import ValidationError
from agent_system.runtime.responses import send_agent_error

# 只对可能携带请求体的方法做检查（GET/DELETE 一般没有 body）
_GUARDED_METHODS = {"POST", "PUT", "PATCH"}


class BodyLimitMiddleware:
    """请求体上限检查：只读取 Content-Length，不缓存、不改写 body。"""

    def __init__(self, app, *, max_bytes: int | None = None):
        """保存下游应用与上限阈值；不传阈值时从全局配置读取。"""
        self.app = app
        self.max_bytes = get_settings().max_body_bytes if max_bytes is None else int(max_bytes)

    async def __call__(self, scope, receive, send):
        """ASGI 入口：仅对受检方法校验 Content-Length，超限时直接返回 400。"""
        if scope["type"] != "http" or scope.get("method") not in _GUARDED_METHODS:
            await self.app(scope, receive, send)
            return

        raw = Headers(scope=scope).get("content-length")
        if raw is not None:
            try:
                content_length = int(raw)
            except ValueError:
                # 头部是伪造或畸形的，交给下游/框架自行处理
                content_length = None
            if content_length is not None and content_length > self.max_bytes:
                # 复用统一错误渲染：400 validation_error，details 带上限与实际大小
                error = ValidationError(
                    "请求体过大",
                    details={"max_bytes": self.max_bytes, "content_length": content_length},
                )
                await send_agent_error(scope, receive, send, error)
                return

        await self.app(scope, receive, send)

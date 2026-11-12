# agent_system/runtime/responses.py
# 供中间件直接渲染统一错误响应：复用步骤 2 的全局处理器，避免重复实现。

from agent_system.errors.handlers import handle_agent_error


async def send_agent_error(scope, receive, send, error, extra_headers: dict | None = None) -> None:
    """把领域异常按统一结构发出；中间件可追加 Retry-After / CORS 等响应头。

    纯 ASGI 中间件位于 ExceptionMiddleware 之外，直接 raise 会落到
    ServerErrorMiddleware 被包装成 internal_error，因此这里显式调用同一个
    全局处理器函数（同一套 hook / payload / X-Request-ID 逻辑）。

    参数：
        scope / receive / send: 当前 ASGI 三元组。
        error: 已分类的领域异常。
        extra_headers: 需要额外附加的响应头（如 Retry-After、CORS 头）。
    """
    from starlette.requests import Request

    request = Request(scope)
    # 复用全局处理器：保证中间件拒绝与业务异常返回完全一致的 body 结构
    response = await handle_agent_error(request, error)
    for key, value in (extra_headers or {}).items():
        response.headers[key] = value
    await response(scope, receive, send)

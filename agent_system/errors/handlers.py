# agent_system/errors/handlers.py
# FastAPI 全局异常处理器：统一错误体、error_raised 日志与 X-Request-ID 回写。
# 不包含重试/降级逻辑，兜底行为由 FallbackHook 决定。
#
# 四个处理器共用同一套流程：
#   取 request_id -> 交给 get_hook().handle() -> 用返回的错误生成 JSON 响应。

import time

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_system.errors import fallback
from agent_system.errors.codes import ErrorCode, http_message_for, normalize_code
from agent_system.errors.exceptions import AgentError, ValidationError
from agent_system.obs.events import log_event
from agent_system.obs.request_id import REQUEST_ID_HEADER, get_request_id


def _request_id(request) -> str:
    """从 scope state 取 request_id，取不到再退回 contextvars，最后兜底 "-"。"""
    return getattr(request.state, "request_id", None) or get_request_id() or "-"


def _session_id(request):
    """取本次请求的 session_id（由中间件解析请求体得到）；未解析到时为 None。"""
    return getattr(request.state, "session_id", None)


def _tool_of(error: AgentError):
    """从错误的 details 中取出工具名，供日志与兜底钩子定位问题工具。"""
    tool = error.details.get("tool")
    return tool if isinstance(tool, str) else None


def _context(request, error: AgentError) -> dict:
    """构造传给 FallbackHook 的上下文：四个字段定位一次错误。"""
    return {
        "request_id": _request_id(request),
        "path": request.url.path,
        "session_id": _session_id(request),
        "tool": _tool_of(error),
    }


def _response(request, error: AgentError) -> JSONResponse:
    """把领域异常渲染成统一 JSON 响应，并回写 X-Request-ID（与 body 保持一致）。"""
    request_id = _request_id(request)
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_payload(request_id),
        headers={REQUEST_ID_HEADER: request_id},
    )


async def handle_agent_error(request, exc: AgentError) -> JSONResponse:
    """领域异常：交给 FallbackHook 处理后返回统一结构。

    钩子返回 AgentError 就用它（允许改写错误分类），返回 None 则沿用原错误。
    """
    hook_result = await fallback.get_hook().handle(exc, _context(request, exc))
    error = hook_result if isinstance(hook_result, AgentError) else exc
    return _response(request, error)


async def handle_validation_error(request, exc: RequestValidationError) -> JSONResponse:
    """请求参数非法：统一为 400 validation_error，details 里列出缺失/非法字段名。"""
    fields = []
    for item in exc.errors():
        # loc 形如 ("body", "message")，去掉 body 前缀只保留字段路径
        loc = [str(part) for part in item.get("loc", ()) if part != "body"]
        if loc:
            fields.append(".".join(loc))
    error = ValidationError(details={"fields": sorted(set(fields))})
    await fallback.get_hook().handle(error, _context(request, error))
    return _response(request, error)


async def handle_http_exception(request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTPException：优先取 detail 中的 code，否则映射为 http_{status}。"""
    detail = exc.detail
    code = None
    message = None
    details = None
    # 支持 `raise HTTPException(400, detail={"code": "...", "message": "..."})` 的写法
    if isinstance(detail, dict):
        if detail.get("code"):
            code = normalize_code(detail["code"])
        if isinstance(detail.get("message"), str):
            message = detail["message"]
        if isinstance(detail.get("details"), dict):
            details = detail["details"]
    if not code:
        # 兜底码：状态码保留原值，语义上仍能区分 404 / 405 等
        code = f"http_{exc.status_code}"
    error = AgentError(
        code,
        message or http_message_for(exc.status_code),
        details=details,
        retryable=False if 400 <= exc.status_code < 500 else True,
    )
    await fallback.get_hook().handle(error, _context(request, error))
    return _response(request, error)


async def handle_unhandled_exception(request, exc: Exception) -> JSONResponse:
    """未捕获异常：统一为 internal_error。

    为兼容步骤 1 已发布的契约，此处仅返回 code + request_id（不新增 message/
    retryable/details），避免破坏既有调用方；cause 只写日志，不进入响应体。
    """
    request_id = _request_id(request)
    error = AgentError(ErrorCode.INTERNAL_ERROR, cause=exc)
    await fallback.get_hook().handle(error, _context(request, error))

    # 异常路径下中间件不会记 request_end（响应还没开始），这里补一条，
    # 保证每个请求都有且只有一条 request_end。
    start = getattr(request.state, "start_perf", None)
    latency_ms = (time.perf_counter() - start) * 1000 if isinstance(start, (int, float)) else 0.0
    log_event(
        "request_end",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        status=500,
        latency_ms=round(latency_ms, 2),
        session_id=_session_id(request),
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "request_id": request_id}},
        headers={REQUEST_ID_HEADER: request_id},
    )


def register_exception_handlers(app) -> None:
    """把四个处理器挂到 app 上。

    注意：AgentError / RequestValidationError / HTTPException 由 ExceptionMiddleware
    处理；裸 Exception 由更外层的 ServerErrorMiddleware 处理，所以它们都发生在
    业务中间件之外——中间件里不要指望 raise 能被这些处理器接住。
    """
    app.add_exception_handler(AgentError, handle_agent_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unhandled_exception)

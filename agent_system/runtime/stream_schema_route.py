"""流式协议自查端点。

本模块位于路由层：暴露 NDJSON 事件 schema 与协议版本，便于前端/运维对齐；
不参与业务对话，也不访问任何外部依赖。
"""

from fastapi import Request
from fastapi.responses import JSONResponse

from agent_system.obs.events import log_event
from agent_system.obs.request_id import get_request_id
from agent_system.stream.schema import STREAM_PROTOCOL_VERSION, list_event_schema


async def stream_schema(request: Request) -> JSONResponse:
    """返回流式协议的事件结构定义。

    参数:
        request: 当前请求对象，用于读取 request_id。

    返回:
        JSON 响应，包含 protocol_version 与全部事件类型 schema。

    说明:
        复用步骤 1 的 log_event 记录 `stream_schema_fetched`，不新增日志字段。
    """
    request_id = getattr(request.state, "request_id", None) or get_request_id()
    log_event("stream_schema_fetched", request_id=request_id)
    return JSONResponse(
        {
            "protocol_version": STREAM_PROTOCOL_VERSION,
            "events": list_event_schema(),
        }
    )


def register_stream_schema_routes(app) -> None:
    """把 `/chat/stream/schema` 注册到应用。

    参数:
        app: FastAPI 应用实例。

    返回:
        无。

    说明:
        该端点默认参与步骤 3 的限流（不属于健康检查豁免路径），如需长期公开可
        将其加入 RATE_LIMIT_EXCLUDE_PATHS。
    """
    app.add_api_route(
        "/chat/stream/schema",
        stream_schema,
        methods=["GET"],
        include_in_schema=False,
    )

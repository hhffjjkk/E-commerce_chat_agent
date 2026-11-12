"""NDJSON 流式协议 schema 定义。

本模块位于协议层：集中描述 `/chat/stream` 的事件类型、必填/可选字段与协议版本，
供 `StreamEmitter` 与 `GET /chat/stream/schema` 自查端点共用，避免两处定义漂移。
"""

from enum import Enum

# 协议版本：新增字段时递增，start 事件会携带该值供前端做兼容判断
STREAM_PROTOCOL_VERSION = "1"


class StreamEventType(str, Enum):
    """NDJSON 事件类型枚举（delta 为兼容旧前端的特殊行）。"""

    DELTA = "delta"
    START = "start"
    ROUTE = "route"
    TOOL_CALL = "tool_call"
    USAGE = "usage"
    CACHE = "cache"
    ERROR = "error"
    DONE = "done"


# 事件 schema 表：required 为本步骤保证必出的字段，optional 为预留/按需字段。
# 注意：delta 行完全保持旧结构，不得注入 event / seq / ts。
_EVENT_SCHEMA: dict[str, dict] = {
    StreamEventType.START.value: {
        "required": ["event", "request_id", "ts", "seq", "session_id"],
        "optional": ["protocol_version", "trace_id", "span_id"],
        "compatibility": "新增事件，旧前端可忽略",
    },
    StreamEventType.ROUTE.value: {
        "required": ["event", "request_id", "ts", "seq"],
        "optional": ["agent", "confidence", "reason", "trace_id", "span_id"],
        "compatibility": "阶段四填充，本步骤只定义结构",
    },
    StreamEventType.TOOL_CALL.value: {
        "required": ["event", "request_id", "ts", "seq"],
        "optional": ["tool", "ok", "latency_ms", "args_keys", "trace_id", "span_id"],
        "compatibility": "阶段三填充，args_keys 只记参数名",
    },
    StreamEventType.USAGE.value: {
        "required": ["event", "request_id", "ts", "seq"],
        "optional": [
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_usd",
            "trace_id",
            "span_id",
        ],
        "compatibility": "阶段三填充",
    },
    StreamEventType.CACHE.value: {
        "required": ["event", "request_id", "ts", "seq"],
        "optional": ["hit", "layer", "key_prefix", "trace_id", "span_id"],
        "compatibility": "阶段五填充，layer 取 session / semantic",
    },
    StreamEventType.ERROR.value: {
        "required": ["event", "request_id", "ts", "seq", "code", "message", "retryable"],
        "optional": ["details", "trace_id", "span_id"],
        "compatibility": "本步骤承接流内异常，结构与步骤 2 的 error.payload 对齐",
    },
    StreamEventType.DONE.value: {
        "required": ["event", "request_id", "ts", "seq"],
        "optional": [
            "status",
            "latency_ms",
            "usage",
            "route",
            "cache",
            "trace_id",
            "span_id",
        ],
        "compatibility": "旧 {done: true} 行保留，本事件为扩展元数据，status 缺省视为 ok",
    },
    StreamEventType.DELTA.value: {
        "required": ["delta"],
        "optional": [],
        "compatibility": "完全保持原样，不注入 event / seq / ts",
    },
}


def list_event_schema() -> dict:
    """返回全部事件的结构定义（深拷贝，调用方可安全修改）。

    返回:
        形如 {事件名: {"required": [...], "optional": [...], "compatibility": str}}
        的字典，事件顺序与协议表格一致。
    """
    return {
        name: {
            "required": list(spec["required"]),
            "optional": list(spec["optional"]),
            "compatibility": spec["compatibility"],
        }
        for name, spec in _EVENT_SCHEMA.items()
    }


def required_fields(event: str) -> list[str]:
    """返回指定事件的必填字段列表（未知事件返回空列表）。

    参数:
        event: 事件名，如 "start"、"error"。

    返回:
        必填字段名列表；未知事件返回 []。
    """
    spec = _EVENT_SCHEMA.get(event)
    return list(spec["required"]) if spec else []

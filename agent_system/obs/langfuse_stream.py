"""流式请求的 Langfuse trace 上下文桥接。

本模块位置在观测层与流式协议层之间：
- 只提供“读取当前请求 trace id”的能力，供 StreamEmitter 在 start / done 事件回填；
- 不在这里创建/更新任何 span，避免在 async generator 的 `yield` 之后操作 Langfuse；
- 根 span 的创建/结束由 langfuse_mapping 在 request_start / request_end 事件中完成。
"""

from agent_system.obs import langfuse_mapping


def current_trace_id() -> str | None:
    """返回当前请求的 Langfuse trace id；未启用时返回 None。

    返回:
        32 位十六进制 trace id，或 None（no-op 模式）。
    """
    return langfuse_mapping.current_trace_id()


def stream_trace_id_provider() -> str | None:
    """供 StreamEmitter 使用的 trace_id_provider 回调。

    返回:
        当前请求 trace id；拿不到时返回 None，emitter 会省略该字段。

    说明:
        provider 只在 `emit_start` / `emit_done` 时被调用；即使流式迭代上下文
        已丢失，emitter 也会缓存首次解析结果，后续事件不再重复读取。
    """
    return current_trace_id()

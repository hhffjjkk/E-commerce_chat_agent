"""把结构化日志事件映射为 Langfuse observation。

设计原则：Langfuse 是消费端，不是侵入端——本模块只读取步骤 1 已埋点的事件，
不修改 `agent.py` / `tools.py`，也不在业务函数上使用 `@observe`。

映射关系：
- `request_start`  -> 根 span（name=agent.request，trace_id 由 request_id 派生）
- `tool_call`      -> 子 span（name=tool.<tool>）
- `llm_call`       -> generation（name=llm.call，记录 model / latency）
- `request_error` / `error_raised` -> 根 span 的 ERROR 状态（仅 code）
- `request_end`    -> 更新并结束根 span
- `rate_limited`   -> 根 span metadata（不单独建 span）
"""

import hashlib
import threading
from contextvars import ContextVar

from agent_system.obs import langfuse_client

# 当前请求的 Langfuse trace id；emitter 通过它回填 NDJSON 的 trace_id
_trace_id_var: ContextVar[str | None] = ContextVar("langfuse_trace_id", default=None)

# 进行中的请求状态：request_id -> {span, trace_id, streaming, metadata, ...}
_states: dict[str, dict] = {}
_lock = threading.Lock()

# 流式请求只创建根 span；yield 之后不再创建/更新任何 observation
_STREAM_PATH = "/chat/stream"


def is_active() -> bool:
    """返回 Langfuse 映射层是否启用（与客户端启用状态一致）。"""
    return langfuse_client.is_enabled()


def current_trace_id() -> str | None:
    """返回当前请求的 Langfuse trace id；未启用或不在请求上下文时返回 None。"""
    return _trace_id_var.get()


def trace_id_for(request_id: str) -> str:
    """把 request_id 稳定派生为 32 位十六进制 trace id。

    参数:
        request_id: 请求 ID（本项目为 16 位 hex）。

    返回:
        合法 trace id：若入参本身已是 32 位 hex 则原样返回，否则取 sha256 前 32 位。

    说明:
        与 Langfuse SDK 的 `create_trace_id(seed=...)` 算法一致，保证不依赖 SDK
        也能得到相同的稳定 ID（评估 runner 与流式回填都复用它）。
    """
    text = str(request_id or "-")
    lowered = text.lower()
    if len(lowered) == 32 and all(char in "0123456789abcdef" for char in lowered):
        return lowered
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _get_state(request_id: str) -> dict | None:
    """读取指定请求的映射状态；不存在时返回 None。"""
    with _lock:
        return _states.get(request_id)


def _start_child(client, state: dict, *, name: str, as_type: str, **kwargs):
    """在根 span 下创建子 observation。

    参数:
        client: Langfuse 客户端（或测试用 fake client）。
        state: 当前请求状态，含根 span 与 trace_id。
        name: observation 名称。
        as_type: observation 类型，span 或 generation。
        **kwargs: 透传给 SDK 的附加字段（metadata / model 等）。

    返回:
        子 observation 对象；根 span 不可用时返回 None。
    """
    root = state.get("span")
    if root is None:
        return None
    # 优先使用 SDK 的 span.start_observation：自动挂到当前根 span 下
    starter = getattr(root, "start_observation", None)
    if callable(starter):
        return starter(name=name, as_type=as_type, **kwargs)
    # 兜底：直接用 client 创建，并显式带上 trace 上下文
    return client.start_observation(
        as_type=as_type,
        name=name,
        trace_context={"trace_id": state["trace_id"]},
        **kwargs,
    )


def _handle_request_start(client, request_id: str, payload: dict) -> None:
    """创建根 span 并缓存请求状态。"""
    trace_id = trace_id_for(request_id)
    path = payload.get("path", "")
    metadata = {
        "request_id": request_id,
        "path": path,
        "method": payload.get("method"),
    }
    span = client.start_observation(
        as_type="span",
        name="agent.request",
        trace_context={"trace_id": trace_id},
        metadata=metadata,
    )
    state = {
        "span": span,
        "trace_id": trace_id,
        "streaming": path == _STREAM_PATH,
        "metadata": metadata,
        "error_code": None,
        "pending_llm": None,
    }
    with _lock:
        _states[request_id] = state
    _trace_id_var.set(trace_id)


def _handle_tool_call(client, request_id: str, payload: dict) -> None:
    """把 tool_call 映射为子 span。"""
    state = _get_state(request_id)
    if state is None or state.get("span") is None:
        return
    # 流式请求：yield 之后禁止创建子 span，只记录摘要供 request_end 使用
    if state["streaming"]:
        state["metadata"].setdefault("tools", []).append(payload.get("tool"))
        return

    tool = payload.get("tool")
    metadata = {
        "tool": tool,
        "ok": payload.get("ok"),
        "latency_ms": payload.get("latency_ms"),
        "args_keys": payload.get("args_keys") or [],
    }
    child = _start_child(client, state, name=f"tool.{tool}", as_type="span", metadata=metadata)
    if child is not None:
        child.update(metadata=metadata)
        child.end()


def _handle_llm_call(client, request_id: str, payload: dict) -> None:
    """把 llm_call 的 start/end 合并映射为一个 generation。"""
    state = _get_state(request_id)
    if state is None or state.get("span") is None:
        return
    # 流式请求：与 tool_call 同理，不在 yield 之后创建 generation
    if state["streaming"]:
        return

    phase = payload.get("phase")
    if phase == "start":
        # start 只缓存上下文，等 end 拿到耗时后再一次性创建 generation
        state["pending_llm"] = {
            "model": payload.get("model"),
            "stream": payload.get("stream"),
        }
        return

    pending = state.get("pending_llm") or {}
    model = payload.get("model") or pending.get("model")
    metadata = {
        "stream": payload.get("stream", pending.get("stream")),
        "ok": payload.get("ok"),
        "latency_ms": payload.get("latency_ms"),
    }
    generation = _start_child(
        client,
        state,
        name="llm.call",
        as_type="generation",
        model=model,
        metadata=metadata,
    )
    if generation is not None:
        generation.update(metadata=metadata)
        generation.end()
    state["pending_llm"] = None


def _handle_error(request_id: str, payload: dict) -> None:
    """在根 span 上记录 ERROR 状态；只写 code，不写原始异常文本。"""
    state = _get_state(request_id)
    if state is None:
        return
    code = payload.get("code") or "internal_error"
    state["error_code"] = code
    state["metadata"]["error_code"] = code
    # 流式请求：yield 之后不更新 span，错误码留到 request_end 的 metadata 中体现
    if not state["streaming"] and state.get("span") is not None:
        state["span"].update(level="ERROR", status_message=code)


def _handle_rate_limited(request_id: str, payload: dict) -> None:
    """把限流命中记录为根 span metadata，不单独建 span。"""
    state = _get_state(request_id)
    if state is None or state.get("span") is None:
        return
    state["metadata"]["rate_limited"] = True
    state["metadata"]["rate_limit_window"] = payload.get("window")
    if not state["streaming"]:
        state["span"].update(metadata={"rate_limited": True})


def _handle_request_end(request_id: str, payload: dict) -> None:
    """更新并结束根 span，然后清理请求状态。"""
    with _lock:
        state = _states.pop(request_id, None)
    if state is None:
        return
    if state.get("span") is not None:
        metadata = dict(state["metadata"])
        metadata["status"] = payload.get("status")
        metadata["latency_ms"] = payload.get("latency_ms")
        state["span"].update(metadata=metadata)
        state["span"].end()
    # 避免 trace id 串到同一线程的下一个请求
    if _trace_id_var.get() == state["trace_id"]:
        _trace_id_var.set(None)


def handle_event(event: str, payload: dict, *, client=None) -> None:
    """把一条结构化日志事件转发到 Langfuse 映射层。

    参数:
        event: 事件名，如 request_start / tool_call。
        payload: 事件字段（已含 request_id）。
        client: 可选的客户端覆盖，仅测试使用；默认使用全局客户端。

    返回:
        无。任何映射异常都会被吞掉，保证观测层不影响业务。
    """
    if client is None:
        if not is_active():
            return
        client = langfuse_client.get_langfuse()

    request_id = str(payload.get("request_id") or "-")
    try:
        if event == "request_start":
            _handle_request_start(client, request_id, payload)
        elif event == "tool_call":
            _handle_tool_call(client, request_id, payload)
        elif event == "llm_call":
            _handle_llm_call(client, request_id, payload)
        elif event in {"request_error", "error_raised"}:
            _handle_error(request_id, payload)
        elif event == "rate_limited":
            _handle_rate_limited(request_id, payload)
        elif event == "request_end":
            _handle_request_end(request_id, payload)
    except Exception:  # noqa: BLE001 - 观测失败绝不能打断请求
        # 清理可能残留的状态，避免内存泄漏
        if event == "request_end":
            with _lock:
                _states.pop(request_id, None)

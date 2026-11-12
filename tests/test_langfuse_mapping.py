"""事件到 Langfuse observation 的映射测试（使用 fake client，不访问网络）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.obs import langfuse_mapping as mapping  # noqa: E402
from agent_system.obs.langfuse_mapping import handle_event, trace_id_for  # noqa: E402


class FakeSpan:
    """记录 update/end 调用；子 span 通过 start_observation 继续委托给 fake client。"""

    def __init__(self, client, name):
        self.client = client
        self.name = name

    def start_observation(self, **kwargs):
        """子 span 创建：交给 client 统一记录。"""
        return self.client.start_observation(**kwargs)

    def update(self, **kwargs):
        """记录一次 span 更新。"""
        self.client.updates.append({"name": self.name, **kwargs})

    def end(self):
        """记录一次 span 结束。"""
        self.client.ended.append(self.name)


class FakeClient:
    """最小 Langfuse 客户端替身，记录所有 observation 调用。"""

    def __init__(self):
        self.observations = []
        self.updates = []
        self.ended = []

    def start_observation(self, **kwargs):
        """记录 observation 创建并返回可操作的 FakeSpan。"""
        self.observations.append(kwargs)
        return FakeSpan(self, kwargs.get("name"))


def _start(client, request_id, path="/chat"):
    """发送 request_start 事件。"""
    handle_event(
        "request_start",
        {"request_id": request_id, "path": path, "method": "POST"},
        client=client,
    )


def _end(client, request_id, status=200):
    """发送 request_end 事件。"""
    handle_event(
        "request_end",
        {"request_id": request_id, "status": status, "latency_ms": 12.5},
        client=client,
    )


def test_trace_id_is_stable_32_hex():
    """trace_id 由 request_id 稳定派生，且为 32 位十六进制。"""
    first = trace_id_for("map-trace-000001")
    second = trace_id_for("map-trace-000001")
    assert first == second
    assert len(first) == 32
    assert all(char in "0123456789abcdef" for char in first)


def test_request_start_and_tool_call_create_spans():
    """request_start 创建根 span，tool_call 创建子 span 并带工具字段。"""
    client = FakeClient()
    request_id = "map-tool-000001"
    _start(client, request_id)
    handle_event(
        "tool_call",
        {
            "request_id": request_id,
            "tool": "query_order",
            "ok": True,
            "latency_ms": 1.23,
            "args_keys": ["order_id"],
        },
        client=client,
    )
    _end(client, request_id)

    names = [item["name"] for item in client.observations]
    assert names == ["agent.request", "tool.query_order"]

    root = client.observations[0]
    assert root["as_type"] == "span"
    assert root["trace_context"]["trace_id"] == trace_id_for(request_id)

    tool = client.observations[1]
    assert tool["metadata"] == {
        "tool": "query_order",
        "ok": True,
        "latency_ms": 1.23,
        "args_keys": ["order_id"],
    }
    # 只记录参数名，没有参数值
    assert "20261101001" not in str(tool)

    root_update = next(item for item in client.updates if item["name"] == "agent.request")
    assert root_update["metadata"]["status"] == 200
    assert root_update["metadata"]["latency_ms"] == 12.5
    assert "agent.request" in client.ended
    assert "tool.query_order" in client.ended


def test_request_error_sets_error_status_without_stack():
    """request_error 只把 code 写进 status_message，不写堆栈/原始异常。"""
    client = FakeClient()
    request_id = "map-error-00001"
    _start(client, request_id)
    handle_event(
        "error_raised",
        {
            "request_id": request_id,
            "code": "llm_failed",
            "retryable": True,
            "cause_type": "TimeoutError",
        },
        client=client,
    )
    _end(client, request_id, status=502)

    error_update = client.updates[0]
    assert error_update["level"] == "ERROR"
    assert error_update["status_message"] == "llm_failed"
    assert "TimeoutError" not in str(client.updates)


def test_llm_call_maps_to_generation():
    """llm_call 的 start/end 合并映射为一个 generation，并记录 model。"""
    client = FakeClient()
    request_id = "map-llm-000001"
    _start(client, request_id)
    handle_event(
        "llm_call",
        {"request_id": request_id, "phase": "start", "model": "deepseek-chat", "stream": False},
        client=client,
    )
    handle_event(
        "llm_call",
        {"request_id": request_id, "phase": "end", "model": None, "stream": False,
         "ok": True, "latency_ms": 30.0},
        client=client,
    )
    _end(client, request_id)

    generation = client.observations[1]
    assert generation["name"] == "llm.call"
    assert generation["as_type"] == "generation"
    assert generation["model"] == "deepseek-chat"
    assert generation["metadata"]["latency_ms"] == 30.0


def test_streaming_skips_child_observations():
    """流式请求只创建根 span，yield 之后不创建子 span，避免 trace 分裂。"""
    client = FakeClient()
    request_id = "map-stream-0001"
    _start(client, request_id, path="/chat/stream")
    handle_event(
        "tool_call",
        {"request_id": request_id, "tool": "query_order", "ok": True,
         "latency_ms": 0.5, "args_keys": ["order_id"]},
        client=client,
    )
    handle_event(
        "llm_call",
        {"request_id": request_id, "phase": "end", "model": "deepseek-chat",
         "stream": True, "ok": True, "latency_ms": 40.0},
        client=client,
    )
    _end(client, request_id)

    assert len(client.observations) == 1
    assert client.observations[0]["name"] == "agent.request"
    # 工具名仍然被记录在根 span 的最终 metadata 中
    assert client.updates[-1]["metadata"]["tools"] == ["query_order"]


def test_mapping_trace_id_context_is_reset():
    """request_end 后当前 trace 上下文应被清空，避免串请求。"""
    client = FakeClient()
    request_id = "map-reset-00001"
    _start(client, request_id)
    assert mapping.current_trace_id() == trace_id_for(request_id)
    _end(client, request_id)
    assert mapping.current_trace_id() is None


def test_emitter_backfills_trace_id_from_provider():
    """启用时 StreamEmitter 在 start/done 回填 trace_id，且 provider 只解析一次。"""
    from agent_system.stream.emitter import StreamEmitter

    calls = []

    def provider():
        calls.append(1)
        return "f" * 32

    emitter = StreamEmitter("rid-emitter-trace", session_id="s1", trace_id_provider=provider)
    start = emitter.emit_start()
    done = emitter.emit_done()

    assert start["trace_id"] == "f" * 32
    assert done["trace_id"] == "f" * 32
    assert len(calls) == 1

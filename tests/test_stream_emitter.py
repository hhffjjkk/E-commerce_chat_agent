"""StreamEmitter 与 wrap_streaming_generator 的单元测试。"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.errors.codes import ErrorCode  # noqa: E402
from agent_system.errors.exceptions import AgentError, LLMError  # noqa: E402
from agent_system.stream.emitter import StreamEmitter, wrap_streaming_generator  # noqa: E402


def test_emit_start_required_fields():
    """start 事件包含必填字段与协议版本，且缺省不含 trace/span。"""
    emitter = StreamEmitter("rid-start-0001", session_id="s1")
    event = emitter.emit_start()

    assert event["event"] == "start"
    assert event["request_id"] == "rid-start-0001"
    assert isinstance(event["ts"], int)
    assert event["seq"] == 0
    assert event["session_id"] == "s1"
    assert event["protocol_version"] == "1"
    assert "trace_id" not in event
    assert "span_id" not in event


def test_optional_fields_are_omitted():
    """可选字段为 None 时不输出，避免空值污染 JSON。"""
    emitter = StreamEmitter("rid-opt-000001", session_id="s1")
    emitter.emit_start()
    route = emitter.emit_route("sales")
    tool = emitter.emit_tool_call("query_order", True, 1.234)
    usage = emitter.emit_usage(3, 5)
    cache = emitter.emit_cache(True, "session")

    assert "confidence" not in route and "reason" not in route
    assert "args_keys" not in tool
    assert "cost_usd" not in usage and usage["total_tokens"] == 8
    assert "key_prefix" not in cache


def test_seq_is_monotonic():
    """连续发射时 seq 从 0 开始单调递增。"""
    emitter = StreamEmitter("rid-seq-000001", session_id="s1")
    events = [
        emitter.emit_start(),
        emitter.emit_route("sales", 0.9, "keyword"),
        emitter.emit_tool_call("query_order", True, 0.5, ["order_id"]),
        emitter.emit_usage(1, 2, 0.0001),
        emitter.emit_cache(False, "semantic", "faq:"),
        emitter.emit_error(AgentError(ErrorCode.LLM_FAILED)),
        emitter.emit_done("failed"),
    ]
    assert [event["seq"] for event in events] == list(range(7))


def test_closed_guard_and_error_path():
    """done 之后禁止继续发射；error → done 的异常路径合法。"""
    emitter = StreamEmitter("rid-guard-0001", session_id="s1")
    emitter.emit_start()
    emitter.emit_error(AgentError(ErrorCode.LLM_FAILED))
    done = emitter.emit_done("failed")
    assert done["status"] == "failed"

    with pytest.raises(RuntimeError):
        emitter.emit_error(AgentError(ErrorCode.LLM_FAILED))
    with pytest.raises(RuntimeError):
        emitter.emit_done("ok")


def test_emit_error_flattens_payload_without_cause():
    """error 事件摊平 code/message/retryable/details，不泄露 cause 内容。"""
    emitter = StreamEmitter("rid-err-000001", session_id="s1")
    emitter.emit_start()
    error = LLMError(cause=TimeoutError("secret token leaked"))
    event = emitter.emit_error(error)

    assert event["event"] == "error"
    assert event["code"] == "llm_failed"
    assert event["message"] == "模型服务调用失败"
    assert event["retryable"] is True
    assert event["details"] == {}
    raw = json.dumps(event, ensure_ascii=False)
    assert "secret token leaked" not in raw
    assert "cause" not in event


def test_trace_fields_when_configured():
    """显式注入 trace_id / span_id 时，所有事件都携带这两个字段。"""
    emitter = StreamEmitter("rid-trace-0001", session_id="s1", trace_id="t-1", span_id="sp-1")
    start = emitter.emit_start()
    assert start["trace_id"] == "t-1"
    assert start["span_id"] == "sp-1"


def test_done_backfills_collected_summaries():
    """done 事件回填此前发射过的 route / usage / cache 摘要。"""
    emitter = StreamEmitter("rid-done-00001", session_id="s1")
    emitter.emit_start()
    emitter.emit_route("sales", 0.8)
    emitter.emit_usage(2, 3, 0.0002)
    emitter.emit_cache(True, "session")
    done = emitter.emit_done("ok", latency_ms=12.345)

    assert done["status"] == "ok"
    assert done["latency_ms"] == pytest.approx(12.345, abs=0.01)
    assert done["route"] == {"agent": "sales", "confidence": 0.8}
    assert done["usage"]["total_tokens"] == 5
    assert done["cache"] == {"hit": True, "layer": "session"}


def _parse(lines: list[str]) -> list[dict]:
    """把 NDJSON 行解析为字典列表。"""
    return [json.loads(line) for line in lines if line.strip()]


def test_wrap_success_order():
    """成功路径：start → 原始行 → 扩展 done，且只有一个 done 事件。"""

    def vendor_like():
        yield json.dumps({"delta": "你好"}, ensure_ascii=False) + "\n"
        yield json.dumps({"done": True}) + "\n"

    emitter = StreamEmitter("rid-wrap-00001", session_id="s1")
    events = _parse(list(wrap_streaming_generator(vendor_like(), emitter)))

    assert events[0]["event"] == "start"
    assert events[1] == {"delta": "你好"}
    assert events[2] == {"done": True}
    assert events[3]["event"] == "done"
    assert events[3]["status"] == "ok"
    assert isinstance(events[3]["latency_ms"], (int, float))
    assert len([e for e in events if e.get("event") == "done"]) == 1


def test_wrap_failure_order():
    """失败路径：start → delta → error → done(failed)。"""

    def failing():
        yield json.dumps({"delta": "部分内容"}, ensure_ascii=False) + "\n"
        raise LLMError(cause=TimeoutError("upstream timeout"))

    emitter = StreamEmitter("rid-wrap-00002", session_id="s1")
    events = _parse(list(wrap_streaming_generator(failing(), emitter)))

    assert [e.get("event") or "delta" for e in events] == ["start", "delta", "error", "done"]
    assert events[2]["code"] == "llm_failed"
    assert events[2]["retryable"] is True
    assert events[3]["status"] == "failed"
    assert "upstream timeout" not in json.dumps(events, ensure_ascii=False)


def test_wrap_non_agent_error_becomes_internal_error():
    """非 AgentError 的流内异常被包装为 internal_error 事件。"""

    def failing():
        yield json.dumps({"delta": "x"}) + "\n"
        raise ValueError("boom")

    emitter = StreamEmitter("rid-wrap-00003", session_id="s1")
    events = _parse(list(wrap_streaming_generator(failing(), emitter)))

    assert events[2]["event"] == "error"
    assert events[2]["code"] == "internal_error"
    assert events[2]["retryable"] is False
    assert events[3]["status"] == "failed"


def test_wrap_does_not_duplicate_existing_done_event():
    """生成器已自带扩展 done 时，不再追加第二个 done。"""

    def with_done():
        yield json.dumps({"delta": "x"}) + "\n"
        yield json.dumps({"event": "done", "request_id": "rid-existing"}) + "\n"

    emitter = StreamEmitter("rid-wrap-00004", session_id="s1")
    events = _parse(list(wrap_streaming_generator(with_done(), emitter)))

    done_events = [e for e in events if e.get("event") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["request_id"] == "rid-existing"


def test_frontend_compatibility_parser():
    """模拟现有前端解析器：只消费 delta 与 done=true，忽略新增事件行。"""

    def vendor_like():
        yield json.dumps({"delta": "你"}, ensure_ascii=False) + "\n"
        yield json.dumps({"delta": "好"}, ensure_ascii=False) + "\n"
        yield json.dumps({"done": True}) + "\n"

    emitter = StreamEmitter("rid-front-00001", session_id="s1")

    # 与 static/index.html 相同的解析口径：只认 delta 与 done
    text = ""
    finished = False
    for line in wrap_streaming_generator(vendor_like(), emitter):
        payload = json.loads(line)
        if "delta" in payload:
            text += payload["delta"]
        elif payload.get("done") is True:
            finished = True

    assert text == "你好"
    assert finished is True


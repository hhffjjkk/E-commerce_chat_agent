# tests/test_request_id.py
# 覆盖请求 ID 复用/生成、响应头、结构化日志、工具埋点、流式 done 事件与异常响应。

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 确保测试始终走 MockLLM + SQLite，避免误连真实 LLM/Redis
for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收进列表，便于断言输出内容而不污染 stdout。"""

    def __init__(self):
        """初始化收集容器。"""
        super().__init__()
        self.records = []

    def emit(self, record):
        """收集一条日志记录（不做任何格式化）。"""
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """在应用 logger 上挂一个临时收集器，测试结束后恢复原状态。"""
    logger = get_logger()
    handler = ListHandler()
    handler.setFormatter(JSONFormatter("test-service"))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture(autouse=True)
def temp_store(tmp_path):
    """把 vendor 的会话存储替换成临时 SQLite，避免污染真实 sessions.db。"""
    from agent_system import app as vendor_app_module
    from agent_system.memory import SessionStore

    original = vendor_app_module.store
    vendor_app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        vendor_app_module.store = original


def _parse_events(handler: ListHandler) -> list[dict]:
    """把收集到的日志按应用的 JSON 格式渲染后再解析，验证「一行一个 JSON」。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _request(method: str, path: str, *, json_body=None, headers=None):
    """用 httpx.ASGITransport 直接调用应用，不占端口、不启动服务器。

    raise_app_exceptions=False：让未捕获异常走应用的异常处理器，
    这样测试才能断言统一错误响应，而不是让异常冒到测试里。
    """

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(run())


@app.post("/__test__/boom")
def _boom():
    """临时路由：演示未捕获异常如何被统一处理（仅测试用）。"""
    raise RuntimeError("boom for test")


def test_reuses_valid_request_id(captured_logs):
    """入站 X-Request-ID 格式合法时应被原样复用，并回写到响应头。"""
    request_id = "trace-abc-123456"
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "s1", "message": "你好"},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id


def test_generates_request_id_when_invalid(captured_logs):
    """入站 ID 含空格等非法字符时应丢弃，并生成 16 位十六进制新 ID。"""
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "s1", "message": "你好"},
        headers={"X-Request-ID": "bad id!"},
    )
    request_id = response.headers["x-request-id"]
    assert request_id != "bad id!"
    assert re.fullmatch(r"[0-9a-f]{16}", request_id)


def test_structured_logs_and_tool_instrumentation(captured_logs):
    """一次正常请求应产生 start / tool_call / end 三条可解析的 JSON 日志。"""
    request_id = "trace-log-123456"
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "s1", "message": "查订单 20261101001"},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200

    events = [e for e in _parse_events(captured_logs) if e.get("request_id") == request_id]
    start = next(e for e in events if e["event"] == "request_start")
    end = next(e for e in events if e["event"] == "request_end")
    tool = next(e for e in events if e["event"] == "tool_call")

    assert start["path"] == "/chat" and start["method"] == "POST"
    assert end["status"] == 200
    assert isinstance(end["latency_ms"], (int, float)) and end["latency_ms"] >= 0
    # session_id 由中间件从请求体解析，用于把日志串到会话维度
    assert end["session_id"] == "s1"
    assert tool["tool"] == "query_order"
    # 只记参数名不记参数值，避免泄露订单号
    assert tool["args_keys"] == ["order_id"]
    assert tool["ok"] is True
    assert tool["session_id"] == "s1"


def test_unhandled_exception_returns_unified_error(captured_logs):
    """未捕获异常应返回统一错误结构，并同时记录 request_error 与 request_end。"""
    request_id = "trace-error-123456"
    response = _request(
        "POST",
        "/__test__/boom",
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 500
    assert response.headers["x-request-id"] == request_id
    assert response.json() == {"error": {"code": "internal_error", "request_id": request_id}}

    events = [e for e in _parse_events(captured_logs) if e.get("request_id") == request_id]
    error = next(e for e in events if e["event"] == "request_error")
    end = next(e for e in events if e["event"] == "request_end")
    assert error["error_type"] == "RuntimeError"
    assert end["status"] == 500


def test_stream_appends_done_event_with_request_id(captured_logs):
    """流式响应：原有事件保持不变，仅在末尾追加一行 event=done 且带 request_id。"""
    request_id = "trace-stream-123456"
    response = _request(
        "POST",
        "/chat/stream",
        json_body={"session_id": "s2", "message": "查订单 20261101001"},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    # 步骤 4 起协议扩展为 start + 原始 delta/done + 扩展 done，且只允许一个 done 事件
    done_events = [line for line in lines if line.get("event") == "done"]
    assert len(done_events) == 1
    assert lines[-1] == done_events[0]
    assert done_events[0]["request_id"] == request_id
    assert done_events[0]["status"] == "ok"
    # 既有协议不能被动过：旧 done 行与 delta 行仍在，首行为新增的 start 事件
    assert {"done": True} in lines
    assert any("delta" in line for line in lines)
    assert lines[0]["event"] == "start"

    # 流式场景下工具日志也必须带上同一个 request_id（验证 contextvars 传播正常）
    tool_events = [
        e
        for e in _parse_events(captured_logs)
        if e["event"] == "tool_call" and e["tool"] == "query_order"
    ]
    assert tool_events and tool_events[-1]["request_id"] == request_id


def test_llm_call_instrumentation(captured_logs):
    """LLM 调用应产生 start / end 两条 llm_call，且 end 带耗时、模型与结果。"""
    from agent_system.agent import LLMClient

    class FakeCompletions:
        """假的 completions 对象，固定返回成功，避免真实网络请求。"""

        def create(self, **kwargs):
            """模拟 create 成功返回。"""
            return {"ok": True}

    # 不走 __init__（避免真实 API Key），直接构造带假 client 的 LLMClient 实例
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    bot.model = "deepseek-chat"

    assert bot._call_llm(stream=False, model="deepseek-chat", messages=[]) == {"ok": True}

    events = [e for e in _parse_events(captured_logs) if e["event"] == "llm_call"]
    assert [e["phase"] for e in events] == ["start", "end"]
    assert events[1]["ok"] is True
    assert events[1]["model"] == "deepseek-chat"
    assert events[1]["stream"] is False
    assert events[1]["latency_ms"] >= 0

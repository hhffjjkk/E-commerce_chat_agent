"""流式协议自查端点测试：版本、8 类事件结构与 stream_schema_fetched 日志。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402  导入时完成 vendor 路径注入
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402

# 与 4.1.2 表格逐项对齐的期望结构
EXPECTED_REQUIRED = {
    "start": ["event", "request_id", "ts", "seq", "session_id"],
    "route": ["event", "request_id", "ts", "seq"],
    "tool_call": ["event", "request_id", "ts", "seq"],
    "usage": ["event", "request_id", "ts", "seq"],
    "cache": ["event", "request_id", "ts", "seq"],
    "error": ["event", "request_id", "ts", "seq", "code", "message", "retryable"],
    "done": ["event", "request_id", "ts", "seq"],
    "delta": ["delta"],
}


class ListHandler(logging.Handler):
    """把日志记录收集到内存，便于断言结构化事件。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """临时挂载一个 JSON 格式化 handler，测试结束后恢复。"""
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


def _events(handler: ListHandler) -> list[dict]:
    """把收集到的日志记录解析为字典列表。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _get(path: str, request_id: str):
    """用 ASGITransport 发起 GET 请求，不依赖真实网络。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def test_stream_schema_endpoint(captured_logs):
    """schema 端点返回协议版本、8 类事件及表格要求的必填字段。"""
    request_id = "schema-00000001"
    response = _get("/chat/stream/schema", request_id)

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id
    body = response.json()
    assert body["protocol_version"] == "1"
    assert list(body["events"]) == [
        "start",
        "route",
        "tool_call",
        "usage",
        "cache",
        "error",
        "done",
        "delta",
    ]

    for event_name, required in EXPECTED_REQUIRED.items():
        assert body["events"][event_name]["required"] == required

    # 预留字段必须出现在可选列表中，供阶段三/四/五填充
    assert body["events"]["start"]["optional"] == ["protocol_version", "trace_id", "span_id"]
    assert "trace_id" in body["events"]["route"]["optional"]
    assert "span_id" in body["events"]["done"]["optional"]

    fetched = [e for e in _events(captured_logs) if e["event"] == "stream_schema_fetched"]
    assert fetched and fetched[-1]["request_id"] == request_id

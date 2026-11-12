"""未配置 Langfuse 时的 no-op 行为测试。

验证目标：未设置密钥时，服务行为与步骤 4 完全一致，且不会触发任何 Langfuse
初始化/SDK 调用。
"""

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

for _key in (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "REDIS_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402  导入时完成 vendor 路径注入与 Langfuse 初始化
from agent_system.obs import langfuse_client  # noqa: E402
from agent_system.obs.langfuse_client import NoOpLangfuse, get_langfuse, is_enabled  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收集到内存，便于断言结构化事件。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """临时挂载 JSON 格式化 handler，测试结束后恢复。"""
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
    """使用临时 SQLite 会话库，避免测试写入 vendor 数据目录。"""
    from agent_system import app as vendor_app_module
    from agent_system.memory import SessionStore

    original = vendor_app_module.store
    vendor_app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        vendor_app_module.store = original


def _events(handler: ListHandler) -> list[dict]:
    """把日志记录解析为字典列表。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _request(method: str, path: str, *, json_body=None, headers=None):
    """通过 ASGITransport 发起请求，不依赖真实网络。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(run())


def test_noop_client_is_used(captured_logs):
    """未配置密钥时：is_enabled=False，get_langfuse 返回 NoOpLangfuse。"""
    assert is_enabled() is False
    client = get_langfuse()
    assert isinstance(client, NoOpLangfuse)
    # no-op 方法应安全返回 None，而不是抛异常
    assert client.start_observation(name="x", as_type="span") is None
    assert client.score(name="x", value=1) is None


def test_init_without_keys_skips_sdk(captured_logs, monkeypatch):
    """未配置密钥时初始化不应触碰 SDK，并记录 langfuse_init enabled=false。"""

    def _boom(settings):
        raise AssertionError("未配置密钥时不应创建 Langfuse 客户端")

    monkeypatch.setattr(langfuse_client, "_create_client", _boom)

    from agent_system.config import get_settings

    assert langfuse_client.init_langfuse(get_settings()) is False
    events = [e for e in _events(captured_logs) if e["event"] == "langfuse_init"]
    assert events and events[-1]["enabled"] is False
    assert events[-1]["reason"] == "not_configured"
    assert "public_key" not in events[-1] and "secret_key" not in events[-1]


def test_chat_has_no_trace_fields(captured_logs):
    """no-op 模式下 /chat 正常返回，且日志/响应不出现 trace 字段。"""
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "noop", "message": "你好"},
        headers={"X-Request-ID": "noop-chat-00001"},
    )
    assert response.status_code == 200
    assert "trace_id" not in response.text


def test_stream_has_no_trace_fields(captured_logs):
    """no-op 模式下 /chat/stream 的 start/done 事件不输出 trace_id / span_id。"""
    response = _request(
        "POST",
        "/chat/stream",
        json_body={"session_id": "noop", "message": "查订单 20261101001"},
        headers={"X-Request-ID": "noop-stream-0001"},
    )
    assert response.status_code == 200

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0]["event"] == "start"
    for line in lines:
        assert "trace_id" not in line
        assert "span_id" not in line

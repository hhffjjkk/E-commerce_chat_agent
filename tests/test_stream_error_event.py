"""流开始后异常的协议承接测试：error 事件与 done(failed) 的顺序与字段。"""

import asyncio
import json
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

from agent_system import app as vendor_app_module  # noqa: E402
from agent_system.errors.codes import ErrorCode  # noqa: E402
from agent_system.errors.exceptions import AgentError  # noqa: E402


class _FailingBot:
    """模拟 LLM 在产出首个 delta 后失败的场景。"""

    def reply_stream(self, message, history=None):
        """先产出一段文本，再抛出 LLMError，触发流内 error 分支。"""
        yield "部分内容"
        raise AgentError(ErrorCode.LLM_FAILED, cause=TimeoutError("upstream timeout"))

    def reply(self, message, history=None):
        """非流式路径在本测试中不使用。"""
        raise AssertionError("reply 不应被调用")


@pytest.fixture(autouse=True)
def temp_store(tmp_path):
    """把 vendor 的会话存储替换为临时 SQLite，避免污染 vendor 数据。"""
    from agent_system.memory import SessionStore

    original = vendor_app_module.store
    vendor_app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        vendor_app_module.store = original


def _stream(request_id: str):
    """向 /chat/stream 发起 POST 请求并返回完整响应。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/chat/stream",
                json={"session_id": "stream-err", "message": "你好"},
                headers={"X-Request-ID": request_id},
            )

    return asyncio.run(run())


def test_stream_error_event_is_emitted(monkeypatch):
    """流中抛出 AgentError 时下发 error 与 done(failed)，状态码保持 200。"""
    monkeypatch.setattr(vendor_app_module, "bot", _FailingBot())
    request_id = "stream-err-000001"
    response = _stream(request_id)

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0]["event"] == "start"
    assert any("delta" in line for line in lines)

    error_event = next(line for line in lines if line.get("event") == "error")
    assert error_event["code"] == "llm_failed"
    assert error_event["retryable"] is True
    assert error_event["details"] == {}
    assert error_event["request_id"] == request_id

    done_events = [line for line in lines if line.get("event") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["status"] == "failed"
    assert done_events[0]["request_id"] == request_id

    # 原始异常信息与堆栈不得进入流
    raw = response.text
    assert "upstream timeout" not in raw
    assert "Traceback" not in raw
    assert '"cause"' not in raw

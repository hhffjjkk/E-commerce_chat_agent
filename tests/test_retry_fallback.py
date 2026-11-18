"""Fallback 单测/集成：LLM 降级、工具失败不降级、流式边界与观察钩子。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.errors.codes import ErrorCode  # noqa: E402
from agent_system.errors.exceptions import AgentError, LLMError, ValidationError  # noqa: E402
from agent_system.errors.fallback import (  # noqa: E402
    NoOpFallbackHook,
    RetryObservingHook,
    register_hook,
)
from agent_system.harness.retry_policy import reset_retry_policy  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """收集结构化日志。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """临时挂载 JSON handler。"""
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
def fresh_retry(tmp_path):
    """重置策略、钩子与会话库。"""
    from agent_system.memory import SessionStore

    reset_retry_policy()
    register_hook(NoOpFallbackHook())
    original_store = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        register_hook(NoOpFallbackHook())
        reset_retry_policy()


def _response(content=None, tool_calls=None):
    """构造 create 返回。"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    """按序返回响应或抛异常，耗尽后重复最后一项。"""

    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        effect = self.effects[min(len(self.calls) - 1, len(self.effects) - 1)]
        if isinstance(effect, BaseException):
            raise effect
        return effect


def _bot(effects):
    """构造 fake LLMClient。"""
    completions = FakeCompletions(effects)
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    bot.model = "fake-model"
    bot._interrupt_probe = None
    bot._last_loop = None
    return bot, completions


def _post(path, request_id):
    """调用 /chat 或 /chat/stream。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                path,
                json={"session_id": "fallback", "message": "你好"},
                headers={"X-Request-ID": request_id},
            )

    return asyncio.run(run())


def _events(handler):
    """解析日志事件。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _enable(monkeypatch, *, fallback=True, attempts=2):
    """开启 Retry；默认开启降级。"""
    monkeypatch.setenv("RETRY_ENABLED", "true")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", str(attempts))
    monkeypatch.setenv("RETRY_INITIAL_BACKOFF_MS", "1")
    monkeypatch.setenv("RETRY_JITTER_RATIO", "0")
    monkeypatch.setenv("RETRY_FALLBACK_ENABLED", "true" if fallback else "false")


def test_llm_exhaustion_returns_fallback_text(monkeypatch, captured_logs):
    """LLM 重试耗尽且开启降级：/chat 200 + fallback_text + degraded_reply。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch, fallback=True, attempts=2)
    bot, _ = _bot([TimeoutError("always")])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("/chat", "fallback-chat-01")
    assert response.status_code == 200
    assert response.json()["reply"] == "系统繁忙，请稍后重试。"

    events = _events(captured_logs)
    degraded = [e for e in events if e["event"] == "degraded_reply"]
    assert len(degraded) == 1
    assert degraded[0]["stream"] is False
    assert len([e for e in events if e["event"] == "retry_exhausted"]) == 1


def test_fallback_disabled_returns_error(monkeypatch, captured_logs):
    """关闭降级时，LLM 耗尽仍返回 500 llm_failed。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch, fallback=False)
    bot, _ = _bot([TimeoutError("always")])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("/chat", "fallback-off-01")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_failed"
    assert not [
        e for e in _events(captured_logs) if e["event"] == "degraded_reply"
    ]


def test_tool_failure_is_not_degraded(monkeypatch, captured_logs):
    """工具失败属于业务失败，即使开启降级也不改写响应。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch, fallback=True)
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="query_order", arguments="not-json"),
    )
    bot, _ = _bot([_response(tool_calls=[tool_call])])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("/chat", "fallback-tool-01")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "tool_failed"
    assert not [
        e for e in _events(captured_logs) if e["event"] == "degraded_reply"
    ]


def test_stream_falls_back_before_first_delta(monkeypatch, captured_logs):
    """流式首次调用耗尽：输出 fallback delta，再正常 done。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch, fallback=True)
    bot, _ = _bot([TimeoutError("always")])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("/chat/stream", "fallback-stream-1")
    assert response.status_code == 200

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0]["event"] == "start"
    assert any(line.get("delta") == "系统繁忙，请稍后重试。" for line in lines)
    assert {"done": True} in lines
    done_events = [line for line in lines if line.get("event") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["status"] == "ok"

    degraded = [
        e for e in _events(captured_logs) if e["event"] == "degraded_reply"
    ]
    assert len(degraded) == 1
    assert degraded[0]["stream"] is True


def test_stream_failure_after_delta_is_not_degraded(monkeypatch):
    """已 yield delta 后的异常不降级，仍走 error + done(failed)。"""

    class FailingBot:
        def reply_stream(self, message, history=None, *, profile=None):
            yield "部分内容"
            raise AgentError(ErrorCode.LLM_FAILED, cause=TimeoutError("late"))

    monkeypatch.setattr(app_module, "bot", FailingBot())
    response = _post("/chat/stream", "fallback-stream-2")
    assert response.status_code == 200

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert not any(line.get("delta") == "系统繁忙，请稍后重试。" for line in lines)
    error_events = [line for line in lines if line.get("event") == "error"]
    assert len(error_events) == 1
    assert error_events[0]["code"] == "llm_failed"
    done_events = [line for line in lines if line.get("event") == "done"]
    assert done_events[0]["status"] == "failed"


def test_retry_observing_hook_records_event(monkeypatch, captured_logs):
    """RetryObservingHook 只观察：记录 retry_exhausted_observed 并原样返回错误。"""
    _enable(monkeypatch, fallback=True)
    hook = RetryObservingHook()
    error = LLMError(cause=TimeoutError("x"))

    returned = asyncio.run(
        hook.handle(error, {"request_id": "hook-1", "path": "/chat"})
    )
    assert returned is error

    observed = [
        e for e in _events(captured_logs) if e["event"] == "retry_exhausted_observed"
    ]
    assert len(observed) == 1
    assert observed[0]["code"] == "llm_failed"

    # 不可重试错误不应记录观察事件
    asyncio.run(hook.handle(ValidationError(), {"request_id": "hook-2", "path": "/chat"}))
    observed = [
        e for e in _events(captured_logs) if e["event"] == "retry_exhausted_observed"
    ]
    assert len(observed) == 1

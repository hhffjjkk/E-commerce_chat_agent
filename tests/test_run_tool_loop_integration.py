"""LoopController 与 LLMClient._run_tool_loop 的集成测试（fake LLM，无真实网络）。"""

import asyncio
import json
import logging
import os
import sys
import time
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
from agent_system import tools  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.harness.loop_controller import (  # noqa: E402
    LoopTerminationReason,
    reset_loop_controller,
)
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """收集结构化日志，便于断言 loop_* 事件。"""

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
def fresh_loop_controller():
    """每个用例都重新读取 LOOP_* 环境变量，避免单例跨用例污染。"""
    reset_loop_controller()
    yield
    reset_loop_controller()


@pytest.fixture(autouse=True)
def temp_store(tmp_path):
    """使用临时 SQLite 会话库，避免写根目录 sessions.db。"""
    from agent_system.memory import SessionStore

    original = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original


def _tool_call(name="query_order", arguments='{"order_id": "20261101001"}'):
    """构造一个 OpenAI 风格的 tool_call 对象。"""
    return SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(tool_calls=None, content=None):
    """构造一个 chat.completions.create 的返回对象。"""
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    """按顺序返回预设响应；耗尽后重复最后一条，便于构造“无限工具调用”。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        """记录调用参数并返回下一个预设响应。"""
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _bot(responses):
    """构造一个绕过 __init__ 的 LLMClient（不触发真实 API Key 校验）。"""
    completions = FakeCompletions(responses)
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    bot.model = "fake-model"
    bot._interrupt_probe = None
    bot._last_loop = None
    return bot, completions


def _events(handler: ListHandler) -> list[dict]:
    """把日志记录解析为字典列表。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _post_chat(request_id: str):
    """通过 ASGITransport 调用 /chat。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/chat",
                json={"session_id": "loop-test", "message": "查订单 20261101001"},
                headers={"X-Request-ID": request_id},
            )

    return asyncio.run(run())


def test_completed_after_two_steps(captured_logs):
    """第 1 轮工具调用、第 2 轮最终回复：COMPLETED，计数正确。"""
    bot, completions = _bot(
        [_response(tool_calls=[_tool_call()]), _response(content="done")]
    )
    messages = LLMClient._run_tool_loop(bot, [{"role": "user", "content": "hi"}])

    assert len(completions.calls) == 2
    assert messages[-1]["role"] == "tool"
    state = bot._last_loop
    assert state.steps == 2
    assert state.tool_calls == 1
    assert state.terminated_reason == LoopTerminationReason.COMPLETED

    events = _events(captured_logs)
    assert len([e for e in events if e["event"] == "loop_started"]) == 1
    terminated = [e for e in events if e["event"] == "loop_terminated"]
    assert len(terminated) == 1
    assert terminated[0]["reason"] == "completed"
    assert terminated[0]["steps"] == 2
    assert terminated[0]["tool_calls"] == 1


def test_default_five_rounds_matches_legacy(captured_logs):
    """默认 5 轮仍请求工具时，保留重构前“静默返回已有 messages”的行为。"""
    bot, completions = _bot([_response(tool_calls=[_tool_call()])])
    messages = LLMClient._run_tool_loop(bot, [{"role": "user", "content": "hi"}])

    assert len(completions.calls) == 5
    assert messages  # 没有抛异常，正常返回
    state = bot._last_loop
    assert state.steps == 5
    assert state.tool_calls == 5
    assert state.terminated_reason == LoopTerminationReason.MAX_STEPS
    terminated = [e for e in _events(captured_logs) if e["event"] == "loop_terminated"]
    assert terminated[-1]["reason"] == "max_steps"


def test_custom_max_steps_raises_loop_exhausted(monkeypatch, captured_logs):
    """LOOP_MAX_STEPS=2 时第 3 次调用被拦截，直接抛 loop_exhausted 并经 /chat 返回 500。"""
    monkeypatch.setenv("LOOP_MAX_STEPS", "2")
    monkeypatch.setenv("LOOP_MAX_TOOL_CALLS", "4")

    bot, completions = _bot([_response(tool_calls=[_tool_call()])])
    with pytest.raises(AgentError) as exc_info:
        LLMClient._run_tool_loop(bot, [{"role": "user", "content": "hi"}])

    error = exc_info.value
    assert error.code == "loop_exhausted"
    assert error.details["reason"] == "max_steps"
    assert error.retryable is False
    assert len(completions.calls) == 2
    assert bot._last_loop.terminated_reason == LoopTerminationReason.MAX_STEPS

    # HTTP 层：替换 app.bot 后走统一错误响应
    http_bot, _ = _bot([_response(tool_calls=[_tool_call()])])
    monkeypatch.setattr(app_module, "bot", http_bot)
    response = _post_chat("loop-exhausted-01")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "loop_exhausted"
    assert body["retryable"] is False
    assert body["details"]["reason"] == "max_steps"


def test_timeout_returns_504(monkeypatch):
    """工具耗时超过 LOOP_TIMEOUT_SECONDS 时返回 504 loop_timeout。"""
    monkeypatch.setenv("LOOP_TIMEOUT_SECONDS", "0.05")

    original_query_order = tools._source.query_order

    def slow_query_order(order_id):
        time.sleep(0.1)
        return original_query_order(order_id)

    monkeypatch.setattr(tools._source, "query_order", slow_query_order)

    bot, completions = _bot([_response(tool_calls=[_tool_call()])])
    monkeypatch.setattr(app_module, "bot", bot)
    response = _post_chat("loop-timeout-0001")

    assert response.status_code == 504
    body = response.json()["error"]
    assert body["code"] == "loop_timeout"
    assert body["retryable"] is True
    assert len(completions.calls) == 1
    assert bot._last_loop.terminated_reason == LoopTerminationReason.TIMEOUT


def test_interrupt_returns_503(monkeypatch):
    """中断探针为 True 时，在首次 LLM 调用前返回 503 loop_interrupted。"""
    monkeypatch.setenv("LOOP_ENABLE_INTERRUPT_CHECK", "true")

    bot, completions = _bot([_response(content="不会走到这里")])
    bot.set_interrupt_probe(lambda: True)
    monkeypatch.setattr(app_module, "bot", bot)
    response = _post_chat("loop-interrupt-01")

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "loop_interrupted"
    assert body["retryable"] is False
    assert completions.calls == []
    assert bot._last_loop.terminated_reason == LoopTerminationReason.INTERRUPTED


def test_tool_error_terminates_with_error_reason(captured_logs):
    """工具异常保持原行为：向调用方抛出，终止原因记录为 ERROR。"""
    bot, completions = _bot([_response(tool_calls=[_tool_call(arguments="not-json")])])
    with pytest.raises(AgentError) as exc_info:
        LLMClient._run_tool_loop(bot, [{"role": "user", "content": "hi"}])

    assert exc_info.value.code == "tool_failed"
    assert len(completions.calls) == 1
    assert bot._last_loop.terminated_reason == LoopTerminationReason.ERROR


def test_mock_path_does_not_emit_loop_events(captured_logs):
    """Mock 模式不进入 _run_tool_loop，因此不产生 loop_* 事件。"""
    response = _post_chat("loop-mock-000001")
    assert response.status_code == 200

    events = _events(captured_logs)
    assert not [e for e in events if str(e.get("event", "")).startswith("loop_")]

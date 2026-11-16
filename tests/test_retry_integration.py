"""Retry 集成测试：LLM 重试、工具重试、写操作不重试与默认关闭回归。"""

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
from agent_system import tools  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.errors.exceptions import DataSourceError  # noqa: E402
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
    """重置 Retry 策略并隔离会话库。"""
    from agent_system.memory import SessionStore

    reset_retry_policy()
    original_store = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        reset_retry_policy()


def _response(content=None, tool_calls=None):
    """构造 create 返回对象。"""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    """按顺序返回响应或抛异常；耗尽后重复最后一项。"""

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
    """构造绕过 __init__ 的 LLMClient。"""
    completions = FakeCompletions(effects)
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    bot.model = "fake-model"
    bot._interrupt_probe = None
    bot._last_loop = None
    return bot, completions


def _post(request_id):
    """调用 /chat。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/chat",
                json={"session_id": "retry", "message": "你好"},
                headers={"X-Request-ID": request_id},
            )

    return asyncio.run(run())


def _events(handler):
    """解析日志事件。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _enable(monkeypatch, *, fallback=False, attempts=3):
    """开启 Retry 的环境变量组合（退避固定 1ms、无抖动）。"""
    monkeypatch.setenv("RETRY_ENABLED", "true")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", str(attempts))
    monkeypatch.setenv("RETRY_INITIAL_BACKOFF_MS", "1")
    monkeypatch.setenv("RETRY_JITTER_RATIO", "0")
    monkeypatch.setenv("RETRY_FALLBACK_ENABLED", "true" if fallback else "false")


def test_llm_retry_until_success(monkeypatch, captured_logs):
    """两次超时后成功：只记录一对 llm_call，retry_attempt 两条。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch)
    bot, completions = _bot(
        [TimeoutError("t1"), TimeoutError("t2"), _response(content="ok")]
    )

    result = bot._call_llm(stream=False, model="fake-model", messages=[])
    assert result.choices[0].message.content == "ok"
    assert len(completions.calls) == 3

    events = _events(captured_logs)
    llm_events = [e for e in events if e["event"] == "llm_call"]
    assert [e["phase"] for e in llm_events] == ["start", "end"]
    assert llm_events[-1]["ok"] is True
    assert len([e for e in events if e["event"] == "retry_attempt"]) == 2


def test_llm_exhaustion_returns_error(monkeypatch, captured_logs):
    """三次均超时且关闭降级：/chat 返回 500 llm_failed，retry_exhausted 一条。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch, fallback=False)
    bot, completions = _bot([TimeoutError("always")])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("retry-exhaust-01")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_failed"
    assert response.json()["error"]["retryable"] is True
    assert len(completions.calls) == 3

    events = _events(captured_logs)
    assert len([e for e in events if e["event"] == "retry_exhausted"]) == 1


def test_read_tool_retries_and_logs_once(monkeypatch, captured_logs):
    """幂等读工具失败两次后成功：tool_call 一条、retry_attempt 两条。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch)

    calls = []

    def flaky_query(order_id):
        calls.append(order_id)
        if len(calls) < 3:
            raise json.JSONDecodeError("bad", "doc", 0)
        return {"found": True, "order_id": order_id}

    monkeypatch.setattr(tools._source, "query_order", flaky_query)
    result = tools.query_order("20261101001")

    assert result["found"] is True
    assert len(calls) == 3
    events = _events(captured_logs)
    tool_events = [e for e in events if e["event"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["ok"] is True
    assert len([e for e in events if e["event"] == "retry_attempt"]) == 2


def test_write_impl_is_never_retried(monkeypatch, captured_logs):
    """写操作实现不经过重试器：失败一次即抛出。"""
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda seconds: None)
    _enable(monkeypatch)
    calls = []

    def fail(order_id, reason):
        calls.append(order_id)
        raise DataSourceError(tool="apply_refund")

    monkeypatch.setattr(tools._source, "apply_refund", fail)
    with pytest.raises(DataSourceError):
        tools._apply_refund_impl("20261101001", "test")

    assert len(calls) == 1
    events = _events(captured_logs)
    assert not [
        e for e in events if e["event"] in {"retry_attempt", "retry_exhausted"}
    ]


def test_default_disabled_has_no_retry(monkeypatch, captured_logs):
    """默认关闭时，LLM 失败一次即返回 502，无 retry 事件。"""
    bot, completions = _bot([TimeoutError("once")])
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("retry-disabled-01")
    assert response.status_code == 502
    assert len(completions.calls) == 1
    events = _events(captured_logs)
    assert not [
        e for e in events if e["event"] in {"retry_attempt", "retry_exhausted"}
    ]

"""RetryExecutor 单测：直通、成功重试、不可重试、耗尽、退避与白名单。"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.errors.codes import ErrorCode  # noqa: E402
from agent_system.errors.exceptions import AgentError, LLMError  # noqa: E402
from agent_system.harness import retry_executor as executor_module  # noqa: E402
from agent_system.harness.retry_executor import (  # noqa: E402
    RetryExecutor,
    compute_backoff_ms,
)
from agent_system.harness.retry_policy import RetryPolicy  # noqa: E402


@pytest.fixture
def events(monkeypatch):
    """收集 retry_* 事件并禁用真实 sleep。"""
    captured = []
    sleeps = []
    monkeypatch.setattr(
        executor_module,
        "log_event",
        lambda event, **fields: captured.append({"event": event, **fields}),
    )
    monkeypatch.setattr(
        executor_module.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )
    return captured, sleeps


def _policy(**overrides) -> RetryPolicy:
    """构造开启重试、无抖动、短退避的策略。"""
    params = {
        "enabled": True,
        "max_attempts": 3,
        "initial_backoff_ms": 100,
        "max_backoff_ms": 1000,
        "backoff_multiplier": 2.0,
        "jitter_ratio": 0.0,
    }
    params.update(overrides)
    return replace(RetryPolicy(), **params)


def test_disabled_runs_once_without_events(events):
    """总开关关闭时直接调用一次，不产生 retry 事件。"""
    captured, sleeps = events
    calls = []
    executor = RetryExecutor(_policy(enabled=False), target="llm")
    assert executor.run(lambda: calls.append(1) or "ok") == "ok"
    assert calls == [1]
    assert captured == []
    assert sleeps == []


def test_retries_until_success(events):
    """前两次可重试失败、第三次成功。"""
    captured, sleeps = events
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LLMError(cause=TimeoutError("t"))
        return "ok"

    assert RetryExecutor(_policy(), target="llm").run(flaky) == "ok"
    assert len(calls) == 3
    attempts = [e for e in captured if e["event"] == "retry_attempt"]
    assert [e["attempt"] for e in attempts] == [1, 2]
    assert sleeps == [0.1, 0.2]
    assert not [e for e in captured if e["event"] == "retry_exhausted"]


def test_non_retryable_stops_immediately(events):
    """不可重试错误码直接抛出，不产生 retry 事件。"""
    captured, sleeps = events
    calls = []

    def bad():
        calls.append(1)
        raise AgentError(ErrorCode.VALIDATION_ERROR)

    with pytest.raises(AgentError):
        RetryExecutor(_policy(), target="llm").run(bad)
    assert len(calls) == 1
    assert captured == []
    assert sleeps == []


def test_exhaustion_records_event_and_reraises(events):
    """达到 max_attempts 后记录一次 retry_exhausted 并原样抛出。"""
    captured, sleeps = events
    calls = []
    original = LLMError(cause=TimeoutError("upstream"))

    def always_fail():
        calls.append(1)
        raise original

    with pytest.raises(LLMError) as exc_info:
        RetryExecutor(_policy(), target="llm").run(always_fail)
    assert exc_info.value is original
    assert len(calls) == 3
    exhausted = [e for e in captured if e["event"] == "retry_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["attempts"] == 3
    assert exhausted[0]["last_error_code"] == "llm_failed"
    assert len(sleeps) == 2


def test_target_whitelist(events):
    """写工具 target 不重试；幂等读工具 target 可重试。"""
    captured, _ = events
    calls = []

    def fail(*args):
        calls.append(1)
        raise AgentError(ErrorCode.TOOL_FAILED)

    with pytest.raises(AgentError):
        RetryExecutor(_policy(), target="tool:apply_refund").run(lambda: fail())
    assert len(calls) == 1
    assert captured == []

    with pytest.raises(AgentError):
        RetryExecutor(_policy(), target="tool:query_order").run(lambda: fail())
    assert len(calls) == 1 + 3  # 一次直通 + 三次重试尝试

    assert len([e for e in captured if e["event"] == "retry_exhausted"]) == 1


def test_custom_is_retryable_override(events):
    """显式 is_retryable 可覆盖默认判定。"""
    captured, _ = events
    calls = []

    def fail():
        calls.append(1)
        raise ValueError("plain")

    with pytest.raises(ValueError):
        # 默认对非 AgentError 不重试
        RetryExecutor(_policy(), target="llm").run(fail)
    assert len(calls) == 1

    calls.clear()
    with pytest.raises(ValueError):
        RetryExecutor(_policy(), target="llm").run(fail, is_retryable=lambda exc: True)
    assert len(calls) == 3


def test_compute_backoff_bounds_and_jitter():
    """指数增长、封顶与抖动边界。"""
    policy = _policy(initial_backoff_ms=100, max_backoff_ms=1000, jitter_ratio=0.2)
    assert compute_backoff_ms(1, policy, rand=lambda: 0.5) == 100
    assert compute_backoff_ms(2, policy, rand=lambda: 0.5) == 200
    assert compute_backoff_ms(5, policy, rand=lambda: 0.5) == 1000

    # 抖动：rand=0 -> -20%，rand=1 -> +20%
    assert compute_backoff_ms(1, policy, rand=lambda: 0.0) == 80
    assert compute_backoff_ms(1, policy, rand=lambda: 1.0) == 120

    with pytest.raises(ValueError):
        compute_backoff_ms(0, policy)

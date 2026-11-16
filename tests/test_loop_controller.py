"""LoopController 单测：终止条件、计数、超时、中断、日志与单例。"""

import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.harness import loop_controller as controller_module  # noqa: E402
from agent_system.harness.loop_controller import (  # noqa: E402
    LoopController,
    LoopPolicy,
    LoopTerminationReason,
    get_loop_controller,
    reset_loop_controller,
)


@pytest.fixture
def events(monkeypatch):
    """收集 Controller 内部 log_event 调用。"""
    captured = []

    def fake_log_event(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(controller_module, "log_event", fake_log_event)
    return captured


def _controller(**overrides) -> LoopController:
    """构造一个合法 Controller，默认 2 轮 / 4 次工具。"""
    params = {
        "max_steps": 2,
        "max_tool_calls": 4,
        "timeout_seconds": 60.0,
    }
    params.update(overrides)
    return LoopController(replace(LoopPolicy(), **params))


def test_begin_initializes_state_and_logs_started(events):
    """begin 初始化计数与开始时间，并记录一条 loop_started。"""
    controller = _controller()
    state = controller.begin()

    assert state.policy is controller.policy
    assert state.steps == 0
    assert state.tool_calls == 0
    assert state.started_at > 0
    assert state.terminated_reason is None
    started = [e for e in events if e["event"] == "loop_started"]
    assert len(started) == 1
    assert started[0]["max_steps"] == 2
    assert re.fullmatch(r"[0-9a-f]{8}", started[0]["policy_id"])


def test_check_max_steps(events):
    """steps 达到 max_steps 时抛 loop_exhausted(max_steps)。"""
    controller = _controller()
    state = controller.begin()
    state.steps = 2

    with pytest.raises(AgentError) as exc_info:
        controller.check(state)
    error = exc_info.value
    assert error.code == "loop_exhausted"
    assert error.http_status == 500
    assert error.retryable is False
    assert error.details == {"reason": "max_steps", "steps": 2, "tool_calls": 0}


def test_check_max_tool_calls(events):
    """tool_calls 达到上限时抛 loop_exhausted(max_tool_calls)。"""
    controller = _controller(max_steps=1, max_tool_calls=2)
    state = controller.begin()
    state.tool_calls = 2

    with pytest.raises(AgentError) as exc_info:
        controller.check(state)
    assert exc_info.value.code == "loop_exhausted"
    assert exc_info.value.details["reason"] == "max_tool_calls"


def test_check_timeout(events):
    """超过 timeout_seconds 抛 loop_timeout，可重试且 HTTP 504。"""
    controller = _controller(timeout_seconds=1.0)
    state = controller.begin()
    state.started_at = time.monotonic() - 10

    with pytest.raises(AgentError) as exc_info:
        controller.check(state)
    error = exc_info.value
    assert error.code == "loop_timeout"
    assert error.http_status == 504
    assert error.retryable is True


def test_interrupt_probe_only_when_enabled(events):
    """中断检查开启时命中探针；关闭时探针完全不被调用。"""
    enabled = _controller(enable_interrupt_check=True)
    state = enabled.begin()
    with pytest.raises(AgentError) as exc_info:
        enabled.check(state, interrupt_probe=lambda: True)
    assert exc_info.value.code == "loop_interrupted"
    assert exc_info.value.http_status == 503
    assert exc_info.value.retryable is False

    disabled = _controller(enable_interrupt_check=False)
    state = disabled.begin()

    def _must_not_call():
        raise AssertionError("中断检查关闭时不应调用探针")

    disabled.check(state, interrupt_probe=_must_not_call)


def test_record_tool_call_and_finish(events):
    """工具计数递增；finish 写入原因与耗时且只记录一次 terminated。"""
    controller = _controller()
    state = controller.begin()
    controller.record_tool_call(state, tool_name="query_order")
    controller.record_tool_call(state, tool_name="query_product")
    assert state.tool_calls == 2

    finished = controller.finish(state, LoopTerminationReason.COMPLETED)
    assert finished.terminated_reason == LoopTerminationReason.COMPLETED
    assert finished.elapsed_ms >= 0
    assert len([e for e in events if e["event"] == "loop_terminated"]) == 1

    # 幂等：重复 finish 不重复记日志
    controller.finish(state, LoopTerminationReason.ERROR)
    assert state.terminated_reason == LoopTerminationReason.COMPLETED
    assert len([e for e in events if e["event"] == "loop_terminated"]) == 1


def test_log_step_event(events):
    """log_step 记录当前轮次与累计工具次数。"""
    controller = _controller()
    state = controller.begin()
    state.steps = 1
    state.tool_calls = 2
    controller.log_step(state)

    steps = [e for e in events if e["event"] == "loop_step"]
    assert len(steps) == 1
    assert steps[0]["step_index"] == 1
    assert steps[0]["tool_calls_so_far"] == 2
    assert steps[0]["elapsed_ms"] >= 0


def test_singleton_and_reset(monkeypatch):
    """单例稳定；reset 后重新读取环境变量。"""
    reset_loop_controller()
    try:
        first = get_loop_controller()
        assert get_loop_controller() is first

        reset_loop_controller()
        monkeypatch.setenv("LOOP_MAX_STEPS", "3")
        monkeypatch.setenv("LOOP_MAX_TOOL_CALLS", "6")
        second = get_loop_controller()
        assert second is not first
        assert second.policy.max_steps == 3
    finally:
        reset_loop_controller()

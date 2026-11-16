"""RetryPolicy 单测：默认值、校验、环境变量、policy_id 与加载事件。"""

import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness import retry_policy as policy_module  # noqa: E402
from agent_system.harness.retry_policy import (  # noqa: E402
    RetryPolicy,
    get_retry_policy,
    reset_retry_policy,
)


@pytest.fixture(autouse=True)
def fresh_policy():
    """每个用例前后重置单例，避免环境变量串扰。"""
    reset_retry_policy()
    yield
    reset_retry_policy()


def test_defaults_are_compatible():
    """默认关闭、3 次尝试、其余参数符合约定。"""
    policy = RetryPolicy()
    policy.validate()
    assert policy.enabled is False
    assert policy.max_attempts == 3
    assert policy.initial_backoff_ms == 300
    assert policy.max_backoff_ms == 5000
    assert policy.backoff_multiplier == 2.0
    assert policy.jitter_ratio == 0.2
    assert re.fullmatch(r"[0-9a-f]{8}", policy.policy_id())


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts": 0},
        {"initial_backoff_ms": 0},
        {"max_backoff_ms": 100, "initial_backoff_ms": 300},
        {"backoff_multiplier": 0.5},
        {"jitter_ratio": -0.1},
        {"jitter_ratio": 1.5},
        {"fallback_text": "  "},
    ],
)
def test_invalid_policies_are_rejected(overrides):
    """非法参数组合应抛 ValueError。"""
    with pytest.raises(ValueError):
        replace(RetryPolicy(), **overrides).validate()


def test_from_env_reads_valid_values(monkeypatch):
    """合法环境变量生效。"""
    monkeypatch.setenv("RETRY_ENABLED", "true")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("RETRY_INITIAL_BACKOFF_MS", "50")
    monkeypatch.setenv("RETRY_MAX_BACKOFF_MS", "1000")
    monkeypatch.setenv("RETRY_BACKOFF_MULTIPLIER", "1.5")
    monkeypatch.setenv("RETRY_JITTER_RATIO", "0")
    monkeypatch.setenv("RETRY_LLM", "false")
    monkeypatch.setenv("RETRY_TOOLS", "false")
    monkeypatch.setenv("RETRY_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("RETRY_FALLBACK_TEXT", "稍后再试")

    policy, invalid = RetryPolicy.from_env()
    assert policy.enabled is True
    assert policy.max_attempts == 4
    assert policy.initial_backoff_ms == 50
    assert policy.jitter_ratio == 0.0
    assert policy.retry_llm is False
    assert policy.retry_tools is False
    assert policy.fallback_enabled is False
    assert policy.fallback_text == "稍后再试"
    assert invalid == []


def test_from_env_falls_back_on_invalid_values(monkeypatch):
    """非法值回退默认并列出变量名。"""
    monkeypatch.setenv("RETRY_ENABLED", "maybe")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("RETRY_INITIAL_BACKOFF_MS", "abc")
    monkeypatch.setenv("RETRY_FALLBACK_TEXT", " ")

    policy, invalid = RetryPolicy.from_env()
    assert policy.enabled is False
    assert policy.max_attempts == 3
    assert policy.initial_backoff_ms == 300
    assert policy.fallback_text == "系统繁忙，请稍后重试。"
    assert set(invalid) == {
        "RETRY_ENABLED",
        "RETRY_MAX_ATTEMPTS",
        "RETRY_INITIAL_BACKOFF_MS",
        "RETRY_FALLBACK_TEXT",
    }


def test_policy_id_is_stable_and_reactive():
    """相同字段 ID 稳定，字段变化 ID 变化。"""
    assert RetryPolicy().policy_id() == RetryPolicy().policy_id()
    assert RetryPolicy().policy_id() != RetryPolicy(max_attempts=4).policy_id()


def test_loaded_event_is_emitted_once_without_fallback_text(monkeypatch):
    """首次 get_retry_policy 记录一条 retry_policy_loaded，且不含 fallback_text。"""
    events = []
    # get_retry_policy 在函数内导入 log_event，直接 patch 源模块属性
    monkeypatch.setattr(
        "agent_system.obs.events.log_event",
        lambda event, **fields: events.append({"event": event, **fields}),
    )

    first = get_retry_policy()
    second = get_retry_policy()
    assert first is second
    loaded = [e for e in events if e["event"] == "retry_policy_loaded"]
    assert len(loaded) == 1
    assert "fallback_text" not in loaded[0]
    assert loaded[0]["enabled"] is False

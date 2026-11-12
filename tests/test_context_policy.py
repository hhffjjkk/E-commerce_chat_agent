"""ContextPolicy 单测：默认值、校验规则、环境变量解析与回退。"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness.context_policy import (  # noqa: E402
    ContextPolicy,
    load_policy_from_env,
)


def test_default_policy_is_valid():
    """默认策略应通过校验，且 policy_id 为 8 位十六进制。"""
    policy = ContextPolicy()
    policy.validate()
    assert re.fullmatch(r"[0-9a-f]{8}", policy.policy_id())


def test_budget_must_leave_minimum_room():
    """输出预留 + 200 必须小于总预算。"""
    with pytest.raises(ValueError):
        ContextPolicy(max_total_tokens=8000, reserve_completion_tokens=7800).validate()


def test_min_keep_cannot_exceed_history_window():
    """保底条数不能超过窗口上限。"""
    with pytest.raises(ValueError):
        ContextPolicy(max_history_messages=4, min_keep_recent_messages=5).validate()


def test_non_positive_integer_fields_are_rejected():
    """整数配置必须为正。"""
    with pytest.raises(ValueError):
        ContextPolicy(max_history_messages=0).validate()
    with pytest.raises(ValueError):
        ContextPolicy(max_user_message_chars=-1).validate()


def test_from_env_reads_valid_values(monkeypatch):
    """合法环境变量应生效。"""
    monkeypatch.setenv("CONTEXT_MAX_HISTORY", "12")
    monkeypatch.setenv("CONTEXT_MAX_TOTAL_TOKENS", "6000")
    monkeypatch.setenv("CONTEXT_RESERVE_COMPLETION", "1000")
    monkeypatch.setenv("CONTEXT_MIN_KEEP_RECENT", "3")
    monkeypatch.setenv("CONTEXT_MAX_USER_CHARS", "2000")
    monkeypatch.setenv("CONTEXT_STABLE_PREFIX", "false")

    policy = ContextPolicy.from_env()
    assert policy.max_history_messages == 12
    assert policy.max_total_tokens == 6000
    assert policy.reserve_completion_tokens == 1000
    assert policy.min_keep_recent_messages == 3
    assert policy.max_user_message_chars == 2000
    assert policy.stable_prefix_mode is False


def test_from_env_falls_back_on_invalid_values(monkeypatch):
    """非法值回退默认且不抛异常，非法项会被记录供调用方打日志。"""
    monkeypatch.setenv("CONTEXT_MAX_HISTORY", "not-a-number")
    monkeypatch.setenv("CONTEXT_MAX_TOTAL_TOKENS", "-5")
    monkeypatch.setenv("CONTEXT_STABLE_PREFIX", "maybe")

    policy, invalid = load_policy_from_env()
    assert policy.max_history_messages == 20
    assert policy.max_total_tokens == 8000
    assert policy.stable_prefix_mode is True
    assert set(invalid) == {
        "CONTEXT_MAX_HISTORY",
        "CONTEXT_MAX_TOTAL_TOKENS",
        "CONTEXT_STABLE_PREFIX",
    }


def test_policy_id_changes_with_policy():
    """策略字段变化会改变 policy_id。"""
    assert ContextPolicy().policy_id() != ContextPolicy(max_history_messages=10).policy_id()

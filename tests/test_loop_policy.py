"""LoopPolicy 单测：默认值、校验、环境变量解析与 policy_id。"""

import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness.loop_controller import LoopPolicy  # noqa: E402


def test_default_policy_is_valid():
    """默认策略应通过校验，policy_id 为 8 位十六进制。"""
    policy = LoopPolicy()
    policy.validate()
    assert policy.max_steps == 5
    assert policy.max_tool_calls == 20
    assert re.fullmatch(r"[0-9a-f]{8}", policy.policy_id())


def test_invalid_values_are_rejected():
    """轮次非正、工具次数小于轮次、超时非正都应报错。"""
    with pytest.raises(ValueError):
        replace(LoopPolicy(), max_steps=0).validate()
    with pytest.raises(ValueError):
        replace(LoopPolicy(), max_steps=5, max_tool_calls=4).validate()
    with pytest.raises(ValueError):
        replace(LoopPolicy(), timeout_seconds=0).validate()


def test_from_env_reads_valid_values(monkeypatch):
    """合法环境变量应生效。"""
    monkeypatch.setenv("LOOP_MAX_STEPS", "3")
    monkeypatch.setenv("LOOP_MAX_TOOL_CALLS", "9")
    monkeypatch.setenv("LOOP_TIMEOUT_SECONDS", "30.5")
    monkeypatch.setenv("LOOP_ENABLE_INTERRUPT_CHECK", "true")

    policy, invalid = LoopPolicy.from_env()
    assert policy.max_steps == 3
    assert policy.max_tool_calls == 9
    assert policy.timeout_seconds == 30.5
    assert policy.enable_interrupt_check is True
    assert invalid == []


def test_from_env_falls_back_on_invalid_values(monkeypatch):
    """非法值回退默认，并列出非法变量名（不记录值）。"""
    monkeypatch.setenv("LOOP_MAX_STEPS", "-2")
    monkeypatch.setenv("LOOP_MAX_TOOL_CALLS", "abc")
    monkeypatch.setenv("LOOP_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("LOOP_ENABLE_INTERRUPT_CHECK", "maybe")

    policy, invalid = LoopPolicy.from_env()
    assert policy.max_steps == 5
    assert policy.max_tool_calls == 20
    assert policy.timeout_seconds == 120.0
    assert policy.enable_interrupt_check is False
    assert set(invalid) == {
        "LOOP_MAX_STEPS",
        "LOOP_MAX_TOOL_CALLS",
        "LOOP_TIMEOUT_SECONDS",
        "LOOP_ENABLE_INTERRUPT_CHECK",
    }


def test_policy_id_is_stable_and_reactive():
    """相同字段 ID 相同，字段变化 ID 变化。"""
    assert LoopPolicy().policy_id() == LoopPolicy().policy_id()
    assert LoopPolicy().policy_id() != LoopPolicy(max_steps=3).policy_id()

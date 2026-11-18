"""压测消息池与场景定义测试。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.load_scenarios import (  # noqa: E402
    BUILTIN_SCENARIOS,
    WRITE_KEYWORDS,
    LoadMessage,
    LoadScenario,
    build_request_sequence,
    load_messages,
    select_scenarios,
)


def test_builtin_scenarios_are_valid():
    """内置场景参数合法，且默认场景包含基线与缓存场景。"""
    for scenario in BUILTIN_SCENARIOS:
        scenario.validate()
        assert scenario.target_path in {"/chat", "/chat/stream"}
        assert 0.0 <= scenario.repeat_ratio <= 1.0
        assert list(scenario.concurrency_levels) == sorted(
            set(scenario.concurrency_levels)
        )

    names = {scenario.name for scenario in BUILTIN_SCENARIOS}
    assert "cache_off" in names
    assert any(name.startswith("cache_on_repeat") for name in names)


def test_message_pool_constraints():
    """消息池数量、Profile 覆盖、可重复消息与写操作黑名单。"""
    messages = load_messages(ROOT / "evals" / "load_messages.jsonl")

    assert len(messages) >= 12
    assert len({message.id for message in messages}) == len(messages)
    assert sum(1 for message in messages if message.repeat_friendly) >= 6

    for profile in ("pre_sales", "order", "after_sales"):
        assert sum(1 for message in messages if message.profile_hint == profile) >= 3

    for message in messages:
        assert not any(keyword in message.message for keyword in WRITE_KEYWORDS)


def test_select_scenarios_all_and_subset():
    """all 展开全部内置场景，重复名称只保留一次。"""
    all_scenarios = select_scenarios(["all"])
    assert len(all_scenarios) == len(BUILTIN_SCENARIOS)

    subset = select_scenarios(["cache_off", "cache_off", "cache_on_repeat_0.5"])
    assert [scenario.name for scenario in subset] == [
        "cache_off",
        "cache_on_repeat_0.5",
    ]


def test_sequence_is_reproducible_and_has_no_duplicates_at_ratio_zero():
    """ratio=0 且请求数不超过池子时，不应出现重复消息；同种子结果一致。"""
    messages = load_messages(ROOT / "evals" / "load_messages.jsonl")
    first = build_request_sequence(messages, count=5, repeat_ratio=0.0, seed=42)
    second = build_request_sequence(messages, count=5, repeat_ratio=0.0, seed=42)

    assert [item.id for item in first] == [item.id for item in second]
    assert len({item.id for item in first}) == 5


def test_repeat_messages_come_from_repeat_friendly():
    """ratio=1 时序列中的消息都应是 repeat_friendly，且存在重复。"""
    messages = load_messages(ROOT / "evals" / "load_messages.jsonl")
    sequence = build_request_sequence(messages, count=10, repeat_ratio=1.0, seed=42)

    assert all(item.repeat_friendly for item in sequence)
    assert len({item.id for item in sequence}) < len(sequence)


def test_repeat_requires_repeat_friendly_message():
    """需要重复消息但池中没有可重复项时必须显式失败。"""
    messages = [LoadMessage("only", "pre_sales", "显示器多少钱", False)]
    with pytest.raises(ValueError, match="repeat_friendly"):
        build_request_sequence(messages, count=4, repeat_ratio=0.5, seed=42)


def test_invalid_scenario_is_rejected():
    """比例越界、路径非法、并发度非递增都应尽早失败。"""
    with pytest.raises(ValueError):
        LoadScenario("bad", "x", True, 1.5, "/chat").validate()
    with pytest.raises(ValueError):
        LoadScenario("bad", "x", True, 0.5, "/unknown").validate()
    with pytest.raises(ValueError):
        LoadScenario("bad", "x", True, 0.5, "/chat", (4, 2)).validate()

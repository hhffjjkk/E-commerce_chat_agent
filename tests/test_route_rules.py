"""规则路由单测：覆盖、互斥、评分、置信度归一化。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness.route_rules import (  # noqa: E402
    RULE_TABLE,
    KeywordRule,
    best_rule,
    score_message,
    validate_rule_table,
)


def test_rule_table_is_valid_and_covers_all_profiles():
    """内置规则表覆盖三个 Profile 且关键词互斥。"""
    validate_rule_table()
    assert {rule.profile for rule in RULE_TABLE} == {
        "pre_sales",
        "order",
        "after_sales",
    }


def test_overlapping_keywords_raise_runtime_error():
    """同一关键词出现在两个 Profile 时，设计期校验失败。"""
    table = (
        KeywordRule(profile="pre_sales", keywords=("退款",)),
        KeywordRule(profile="after_sales", keywords=("退款",)),
    )
    with pytest.raises(RuntimeError):
        validate_rule_table(table)


def test_unknown_profile_in_rule_raises():
    """规则引用未知 Profile 时报错。"""
    with pytest.raises(RuntimeError):
        validate_rule_table((KeywordRule(profile="unknown", keywords=("测试",)),))


def test_missing_profile_rule_raises():
    """缺少某个 Profile 的规则时报错。"""
    with pytest.raises(RuntimeError):
        validate_rule_table((KeywordRule(profile="pre_sales", keywords=("商品",)),))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("这个商品多少钱", "pre_sales"),
        ("我的订单到哪了", "order"),
        ("我要退款", "after_sales"),
    ],
)
def test_typical_messages_route_to_expected_profile(message, expected):
    """典型业务消息命中对应 Profile。"""
    scores = score_message(message)
    assert max(scores, key=scores.get) == expected
    agent, confidence = best_rule(message)
    assert agent == expected
    assert 0 < confidence < 1


def test_unrelated_message_scores_zero():
    """无关消息所有得分为 0，best_rule 返回 None。"""
    scores = score_message("今天天气不错")
    assert all(score == 0 for score in scores.values())
    assert best_rule("今天天气不错") == (None, 0.0)


def test_tie_returns_none():
    """两个 Profile 并列最高分时无法决策，返回 None。"""
    assert best_rule("商品退款") == (None, 0.0)


def test_confidence_is_normalized_and_monotonic():
    """置信度随命中数单调递增且始终小于 1。"""
    one_hit = best_rule("我要退款")[1]
    two_hits = best_rule("我要退款退货")[1]
    assert 0 < one_hit < two_hits < 1.0

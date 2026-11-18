"""统计口径测试：通过率、P50/P95、成本、分组与缓存命中率。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.metrics import (  # noqa: E402
    TaskResult,
    compute_cost,
    compute_report,
    compute_task_result,
    percentile,
)
from agent_system.eval.task_schema import ExpectSpec, TaskCase  # noqa: E402


def _result(task_id: str, passed: bool, latency: float, *, category="route_rule_hit", profile="pre_sales") -> TaskResult:
    """构造一条最小任务结果。"""
    return TaskResult(
        task_id=task_id,
        category=category,
        passed=passed,
        route_agent=profile,
        llm_calls=2,
        prompt_tokens=100,
        completion_tokens=20,
        cost_usd=None,
        latency_ms=latency,
        tools_used=["query_product"],
        cache_hit=False,
        errors=[],
        evaluator_results={"route_match": passed},
        failure_reason=None if passed else "路由匹配未通过",
    )


def test_nearest_rank_percentile():
    """nearest-rank：5 个样本 p50=300、p95=500；4 个样本 p50=200、p95=400。"""
    five = [100, 200, 300, 400, 500]
    assert percentile(five, 50) == 300
    assert percentile(five, 95) == 500

    four = [100, 200, 300, 400]
    assert percentile(four, 50) == 200
    assert percentile(four, 95) == 400
    assert percentile([], 95) == 0.0


def test_cost_uses_configured_unit_price():
    """1000 输入 + 500 输出，单价 2/8 -> 成本 6；未配置单价返回 None。"""
    assert compute_cost(1000, 500, 2.0, 8.0) == 6.0
    assert compute_cost(1000, 500, 0.0, 0.0) is None


def test_pass_rate_and_latency_stats():
    """3 个任务 2 个通过 -> pass_rate=2/3，延迟取任务级 P50/P95。"""
    results = [
        _result("a", True, 100),
        _result("b", True, 200),
        _result("c", False, 300),
    ]
    report = compute_report(
        mode="mock",
        seed=42,
        started_at="2026-11-18T10:00:00",
        finished_at="2026-11-18T10:01:00",
        results=results,
        skipped_tasks=[],
        cache_hit_count=0,
        cache_miss_count=0,
    )

    assert report.total == 3
    assert report.executed == 3
    assert report.passed == 2
    assert report.pass_rate == round(2 / 3, 4)
    assert report.latency.p50_ms == 200
    assert report.latency.p95_ms == 300
    assert report.llm_calls_total == 6


def test_by_category_and_profile_p95():
    """分组聚合：总数/通过数/P95 按组独立计算。"""
    results = [
        _result("a", True, 100, category="cache_hit", profile="pre_sales"),
        _result("b", True, 500, category="cache_hit", profile="pre_sales"),
        _result("c", False, 300, category="error_path", profile="order"),
    ]
    report = compute_report(
        mode="mock",
        seed=42,
        started_at="s",
        finished_at="f",
        results=results,
        skipped_tasks=[],
        cache_hit_count=0,
        cache_miss_count=0,
    )

    cache = report.by_category["cache_hit"]
    assert cache.total == 2
    assert cache.passed == 2
    assert cache.p95_latency_ms == 500
    assert cache.avg_llm_calls == 2.0

    order = report.by_profile["order"]
    assert order.total == 1
    assert order.pass_rate == 0.0
    assert order.p95_latency_ms == 300


def test_cache_hit_rate_handles_zero_and_partial():
    """命中率 = 命中/(命中+未命中)；无请求时给 0.0 而不是除零异常。"""
    results = [_result("a", True, 100)]
    partial = compute_report(
        mode="mock",
        seed=42,
        started_at="s",
        finished_at="f",
        results=results,
        skipped_tasks=[],
        cache_hit_count=2,
        cache_miss_count=4,
    )
    empty = compute_report(
        mode="mock",
        seed=42,
        started_at="s",
        finished_at="f",
        results=[],
        skipped_tasks=[],
        cache_hit_count=0,
        cache_miss_count=0,
    )

    assert partial.cache_hit_rate == round(2 / 6, 4)
    assert empty.cache_hit_rate == 0.0
    assert empty.pass_rate == 0.0


def test_compute_task_result_applies_only_configured_evaluators():
    """未配置的期望不参与判定；错误码不在白名单时任务失败。"""
    task = TaskCase(
        id="t1",
        category="cache_hit",
        session_id="eval-t1",
        messages=("显示器多少钱",),
        expect=ExpectSpec(route_agent="pre_sales", cache_hit=True, contains=("1899",)),
    )
    snapshot = {
        "route": {"agent": "pre_sales"},
        "tools_used": ["query_product"],
        "cache_hit": 1,
        "errors": [],
        "llm_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }

    passed = compute_task_result(task=task, snapshot=snapshot, reply="价格 1899 元", latency_ms=12.0)
    assert passed.passed is True
    assert passed.cache_hit is True
    assert passed.failure_reason is None
    assert set(passed.evaluator_results) == {
        "route_match",
        "contains_keywords",
        "no_write_tools",
        "cache_hit_expected",
        "errors_allowed",
    }

    failed = compute_task_result(
        task=task,
        snapshot={**snapshot, "errors": ["tool_failed"]},
        reply="价格 1899 元",
        latency_ms=12.0,
    )
    assert failed.passed is False
    assert "错误码白名单未通过" in failed.failure_reason

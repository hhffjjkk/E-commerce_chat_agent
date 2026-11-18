"""压测指标聚合测试：QPS、百分位、错误率、命中率与节省估算。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.load_metrics import (  # noqa: E402
    RequestRecord,
    aggregate_level,
    compute_qps,
    compute_wall_clock_ms,
    estimate_saved_llm_calls,
    nearest_rank_percentile,
)


def _record(
    *,
    started=0.0,
    finished=2.0,
    status=200,
    latency=10.0,
    ttfb=None,
    cache_hit=False,
    llm_calls=0,
    error=None,
) -> RequestRecord:
    """构造一条请求记录。"""
    return RequestRecord(
        started_at=started,
        finished_at=finished,
        status_code=status,
        latency_ms=latency,
        ttfb_ms=ttfb,
        cache_hit=cache_hit,
        llm_calls=llm_calls,
        error=error,
    )


def test_qps_uses_success_and_wall_clock():
    """100 个成功请求、耗时 2 秒 -> QPS 50；耗时 0 返回 0。"""
    assert compute_qps(100, 2000.0) == 50.0
    assert compute_qps(100, 0.0) == 0.0
    assert compute_qps(0, 2000.0) == 0.0
    # perf_counter 时间戳单位是秒，聚合时必须换算成毫秒
    assert compute_wall_clock_ms([_record(started=0.0, finished=2.0)]) == 2000.0


def test_nearest_rank_percentiles():
    """延迟 10..100 的 nearest-rank 口径。"""
    values = list(range(10, 101, 10))
    assert nearest_rank_percentile(values, 50) == 50
    assert nearest_rank_percentile(values, 95) == 100
    assert nearest_rank_percentile(values, 99) == 100
    assert nearest_rank_percentile([], 95) == 0.0


def test_aggregate_error_rate_and_cache_hit_rate():
    """10 个请求 2 个失败 -> 0.2；30 个命中 / 100 -> 0.3。"""
    records = [_record() for _ in range(8)] + [
        _record(status=500, error="http_500"),
        _record(status=0, error="timeout"),
    ]
    stats = aggregate_level(records, concurrency=4)
    assert stats.total_requests == 10
    assert stats.success == 8
    assert stats.errors == 2
    assert stats.error_rate == 0.2

    cache_records = [
        _record(cache_hit=index < 30) for index in range(100)
    ]
    cache_stats = aggregate_level(cache_records, concurrency=8)
    assert cache_stats.cache_hit_rate == 0.3

    empty = aggregate_level([], concurrency=1)
    assert empty.cache_hit_rate == 0.0
    assert empty.error_rate == 0.0
    assert empty.qps == 0.0


def test_ttfb_only_aggregates_records_with_ttfb():
    """TTFB 只统计带该字段的请求；全无时字段为 None。"""
    records = [
        _record(ttfb=10.0),
        _record(ttfb=20.0),
        _record(ttfb=30.0),
        _record(ttfb=40.0),
        _record(ttfb=50.0),
        _record(ttfb=None),
    ]
    stats = aggregate_level(records, concurrency=2)
    assert stats.ttfb_p50_ms == 30
    assert stats.ttfb_p95_ms == 50

    no_ttfb = aggregate_level([_record()], concurrency=1)
    assert no_ttfb.ttfb_p50_ms is None
    assert no_ttfb.ttfb_p95_ms is None


def test_llm_savings_requires_baseline():
    """命中 30 次 × 基线平均 1.5 次调用 = 45；无基线返回 None。"""
    assert estimate_saved_llm_calls(30, 1.5) == 45.0
    assert estimate_saved_llm_calls(30, None) is None

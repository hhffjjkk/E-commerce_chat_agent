"""压测报告测试：区块完整性、基线对比与摘要口径。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.load_metrics import (  # noqa: E402
    LevelStats,
    LoadScenarioResult,
)
from agent_system.eval.load_reporter import (  # noqa: E402
    build_summary_paragraph,
    write_json_report,
    write_markdown_report,
)
from agent_system.eval.load_scenarios import (  # noqa: E402
    SCENARIO_CACHE_OFF,
    SCENARIO_CACHE_ON_REPEAT_05,
)


def _level(
    concurrency: int,
    *,
    qps: float,
    p95: float,
    hit_rate: float,
    errors: int = 0,
) -> LevelStats:
    """构造一个并发度的统计。"""
    total = 100
    return LevelStats(
        concurrency=concurrency,
        total_requests=total,
        success=total - errors,
        errors=errors,
        error_rate=errors / total,
        qps=qps,
        latency_p50_ms=p95 / 2,
        latency_p95_ms=p95,
        latency_p99_ms=p95 * 1.2,
        latency_max_ms=p95 * 1.5,
        ttfb_p50_ms=None,
        ttfb_p95_ms=None,
        cache_hit_rate=hit_rate,
        avg_llm_calls_per_request=0.0,
        total_llm_calls=0,
    )


def _result(scenario, level: LevelStats, *, aborted=None) -> LoadScenarioResult:
    """构造场景结果。"""
    return LoadScenarioResult(
        scenario=scenario,
        levels=[level],
        wall_clock_ms=1000.0,
        aborted=aborted,
        mode="mock",
        seed=42,
        environment={"backend": "fake_redis", "fake_redis": True, "key_count": 1},
    )


def _baseline_and_enabled():
    """构造基线 + 缓存两组结果（最高并发 8）。"""
    baseline = _result(
        SCENARIO_CACHE_OFF,
        _level(8, qps=100.0, p95=200.0, hit_rate=0.0),
    )
    enabled = _result(
        SCENARIO_CACHE_ON_REPEAT_05,
        _level(8, qps=150.0, p95=100.0, hit_rate=0.5),
    )
    return [baseline, enabled]


def test_markdown_contains_all_sections_and_comparison(tmp_path):
    """Markdown 包含 6 个区块、收益对比与中文摘要。"""
    results = _baseline_and_enabled()
    path = write_markdown_report(
        results,
        mode="mock",
        seed=42,
        started_at="2026-11-18T10:00:00",
        finished_at="2026-11-18T10:01:00",
        out_dir=tmp_path,
    )
    text = path.read_text(encoding="utf-8")

    for title in (
        "## 1. 运行信息",
        "## 2. 场景对比",
        "## 3. 并发度明细",
        "## 4. 缓存收益对比",
        "## 5. 摘要",
        "## 6. 失败/中止项",
    ):
        assert title in text

    assert "提升 50.0%" in text
    assert "降低 50.0%" in text
    assert "缓存命中率 50.0%" in text
    assert "本次压测在 mock 模式下运行" in text
    assert "LLM 调用量恒为 0" in text
    # 必须披露 Mock 下的模拟编排耗时，避免把绝对 QPS 当成生产容量
    assert "模拟编排耗时" in text
    assert "N/A" in text  # 基线场景缓存关闭，命中率显示 N/A
    # 中文标签，不泄露内部字段名
    assert "cache_hit_rate" not in text
    assert "latency_p95_ms" not in text


def test_summary_requires_baseline(tmp_path):
    """只跑缓存场景时，摘要与收益段都明确提示缺少基线。"""
    only_enabled = [
        _result(
            SCENARIO_CACHE_ON_REPEAT_05,
            _level(8, qps=150.0, p95=100.0, hit_rate=0.5),
        )
    ]
    assert "未运行基线场景，无法生成对比摘要" in build_summary_paragraph(
        only_enabled, mode="mock"
    )

    path = write_markdown_report(
        only_enabled,
        mode="mock",
        seed=42,
        started_at="2026-11-18T10:00:00",
        finished_at="2026-11-18T10:01:00",
        out_dir=tmp_path,
    )
    assert "未运行基线场景" in path.read_text(encoding="utf-8")


def test_summary_reports_aborted_scenarios():
    """摘要必须列出中止场景，不能隐藏失败。"""
    baseline = _result(
        SCENARIO_CACHE_OFF, _level(8, qps=100.0, p95=200.0, hit_rate=0.0)
    )
    aborted = _result(
        SCENARIO_CACHE_ON_REPEAT_05,
        _level(8, qps=0.0, p95=0.0, hit_rate=0.0, errors=100),
        aborted="error_rate_exceeded",
    )

    summary = build_summary_paragraph([baseline, aborted], mode="mock")

    assert "失败/中止" in summary
    assert "error_rate_exceeded" in summary


def test_json_report_is_a_list_of_scenarios(tmp_path):
    """JSON 根节点是场景列表，包含 levels 与运行上下文。"""
    results = _baseline_and_enabled()
    path = write_json_report(
        results,
        mode="mock",
        seed=42,
        started_at="2026-11-18T10:00:00",
        finished_at="2026-11-18T10:01:00",
        out_dir=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert isinstance(payload, list)
    assert len(payload) == 2
    assert payload[0]["scenario"]["name"] == "cache_off"
    assert payload[0]["levels"][0]["qps"] == 100.0
    assert payload[1]["mode"] == "mock"
    assert payload[1]["seed"] == 42
    assert path.name.startswith("load-")
    assert path.suffix == ".json"

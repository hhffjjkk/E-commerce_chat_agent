"""summarize_metrics 脚本契约测试：指标完整性、缺失处理与格式。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import summarize_metrics  # noqa: E402

EXPECTED_IDS = [f"M-{index:02d}" for index in range(1, 24)]


def _eval_payload(pass_rate=1.0, route_rate=1.0, error_rate=1.0):
    """构造最小 EvalReport JSON。"""
    return {
        "mode": "mock",
        "seed": 42,
        "total": 27,
        "executed": 22,
        "passed": round(pass_rate * 22),
        "skipped": 5,
        "pass_rate": pass_rate,
        "llm_calls_total": 0,
        "by_category": {
            "route_rule_hit": {"total": 5, "passed": 5, "pass_rate": route_rate},
            "error_path": {"total": 3, "passed": 3, "pass_rate": error_rate},
        },
    }


def _load_payload():
    """构造最小 load 报告 JSON（两个场景）。"""
    return [
        {
            "scenario": {"name": "cache_off"},
            "levels": [
                {
                    "concurrency": 8,
                    "total_requests": 200,
                    "qps": 100.0,
                    "latency_p95_ms": 200.0,
                    "cache_hit_rate": 0.0,
                }
            ],
        },
        {
            "scenario": {"name": "cache_on_repeat_0.5"},
            "levels": [
                {
                    "concurrency": 8,
                    "total_requests": 200,
                    "qps": 150.0,
                    "latency_p95_ms": 100.0,
                    "cache_hit_rate": 0.5,
                }
            ],
        },
    ]


def _write_reports(tmp_path: Path) -> None:
    """写入一整套最小报告。"""
    (tmp_path / "eval-20261118-000000-mock.json").write_text(
        json.dumps(_eval_payload()), encoding="utf-8"
    )
    (tmp_path / "load-20261118-000000-mock.json").write_text(
        json.dumps(_load_payload()), encoding="utf-8"
    )
    (tmp_path / "baseline-no_multi_agent-20261118-000000.json").write_text(
        json.dumps(_eval_payload(pass_rate=0.5909, route_rate=0.4)), encoding="utf-8"
    )
    (tmp_path / "baseline-no_nmsg_cache-20261118-000000.json").write_text(
        json.dumps(_eval_payload(pass_rate=0.8636)), encoding="utf-8"
    )
    (tmp_path / "baseline-no_retry-20261118-000000.json").write_text(
        json.dumps(_eval_payload()), encoding="utf-8"
    )
    (tmp_path / "baseline-no_guardrails-20261118-000000.json").write_text(
        json.dumps(_eval_payload()), encoding="utf-8"
    )


def test_all_predefined_metrics_are_present(tmp_path):
    """完整报告输入时，M-01 至 M-23 全部存在且关键值正确。"""
    _write_reports(tmp_path)
    entries = summarize_metrics.build_entries(tmp_path, "abc123")
    by_id = {entry["id"]: entry for entry in entries}

    assert list(by_id) == EXPECTED_IDS
    assert by_id["M-03"]["value"] == 40.0
    assert by_id["M-04"]["value"] == 60.0
    assert by_id["M-06"]["value"] == 50.0
    assert by_id["M-07"]["value"] == 50.0
    assert by_id["M-05"]["value"] == 50.0
    assert by_id["M-22"]["value"] == 40.9
    assert by_id["M-23"]["value"] == 13.6
    assert by_id["M-10"]["display"] == "0 次/任务"


def test_missing_reports_mark_null_and_explain(tmp_path):
    """缺少 load/baseline 时对应指标为 null，且 caveat 明确“数据缺失”。"""
    (tmp_path / "eval-20261118-000000-mock.json").write_text(
        json.dumps(_eval_payload()), encoding="utf-8"
    )
    entries = summarize_metrics.build_entries(tmp_path, "abc123")
    by_id = {entry["id"]: entry for entry in entries}

    assert by_id["M-01"]["value"] == 100.0
    for entry_id in ("M-05", "M-06", "M-08", "M-18"):
        assert by_id[entry_id]["value"] is None
        assert "数据缺失" in by_id[entry_id]["caveat"]
    assert by_id["M-03"]["value"] is None
    assert "数据缺失" in by_id["M-03"]["caveat"]


def test_markdown_matches_json(tmp_path):
    """Markdown 表格与 JSON 条目一一对应。"""
    _write_reports(tmp_path)
    entries = summarize_metrics.build_entries(tmp_path, "abc123")
    out_dir = tmp_path / "resume"
    json_path, md_path = summarize_metrics.write_index(
        entries, out_dir=out_dir, version="abc123"
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    text = md_path.read_text(encoding="utf-8")

    assert payload["code_version"] == "abc123"
    assert len(payload["entries"]) == len(EXPECTED_IDS)
    for entry in payload["entries"]:
        assert entry["id"] in text
        assert entry["display"] in text
    assert text.count("| M-") == len(EXPECTED_IDS)


def test_number_formatting(tmp_path):
    """百分比一位小数；次数不带小数。"""
    _write_reports(tmp_path)
    entries = summarize_metrics.build_entries(tmp_path, "abc123")
    by_id = {entry["id"]: entry for entry in entries}

    assert by_id["M-03"]["display"] == "40.0%"
    assert by_id["M-06"]["display"] == "50.0%"
    assert by_id["M-10"]["display"] == "0 次/任务"
    assert by_id["M-20"]["display"] == "27 个任务"

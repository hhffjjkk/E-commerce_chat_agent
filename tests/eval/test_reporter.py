"""报告生成测试：JSON 结构、Markdown 区块与摘要口径。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.metrics import TaskResult, compute_report  # noqa: E402
from agent_system.eval.reporter import write_json, write_markdown  # noqa: E402


def _report():
    """构造一份包含 1 个失败任务的报告。"""
    results = [
        TaskResult(
            task_id="pass-01",
            category="route_rule_hit",
            passed=True,
            route_agent="pre_sales",
            llm_calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=None,
            latency_ms=120.0,
            tools_used=["query_product"],
            cache_hit=False,
            errors=[],
            evaluator_results={"route_match": True, "no_write_tools": True},
            failure_reason=None,
            notes="售前价格查询",
        ),
        TaskResult(
            task_id="fail-01",
            category="cache_miss_not_cached",
            passed=False,
            route_agent="after_sales",
            llm_calls=1,
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=None,
            latency_ms=240.0,
            tools_used=[],
            cache_hit=True,
            errors=["tool_failed"],
            evaluator_results={"route_match": True, "cache_hit_expected": False},
            failure_reason="缓存命中未通过",
            notes="售后消息不应命中缓存",
        ),
    ]
    return compute_report(
        mode="mock",
        seed=42,
        started_at="2026-11-18T10:00:00",
        finished_at="2026-11-18T10:01:00",
        results=results,
        skipped_tasks=[
            {
                "task_id": "skip-01",
                "category": "pending_flow",
                "reason": "仅真实 LLM 模式运行",
                "notes": "确认流程",
            }
        ],
        cache_hit_count=1,
        cache_miss_count=2,
    )


def test_markdown_contains_required_sections(tmp_path):
    """Markdown 必须包含 6 个区块、失败清单、任务说明与摘要。"""
    path = write_markdown(_report(), tmp_path)
    text = path.read_text(encoding="utf-8")

    for title in (
        "## 1. 运行信息",
        "## 2. 核心指标",
        "## 3. 按分类统计",
        "## 4. 按 Profile 统计",
        "## 5. 失败任务清单",
        "## 6. 缓存命中节省",
        "## 7. 任务清单",
        "## 摘要",
    ):
        assert title in text

    assert "fail-01" in text
    assert "售后消息不应命中缓存" in text
    assert "失败任务 1 个" in text
    assert "cache_miss_not_cached" in text
    # 未配置单价时必须明确写出，而不是显示 0
    assert "未配置单价" in text
    # 中文标签，不泄露内部字段名
    assert "by_category" not in text
    assert "pass_rate" not in text


def test_json_roundtrip_matches_report(tmp_path):
    """JSON 报告可反序列化，关键字段与 EvalReport 一致。"""
    report = _report()
    path = write_json(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["mode"] == "mock"
    assert payload["seed"] == 42
    assert payload["executed"] == 2
    assert payload["skipped"] == 1
    assert payload["pass_rate"] == 0.5
    assert payload["results"][0]["task_id"] == "pass-01"
    assert payload["skipped_tasks"][0]["task_id"] == "skip-01"
    assert payload["cache_hit_rate"] == round(1 / 3, 4)


def test_files_are_written_as_a_pair(tmp_path):
    """JSON 与 Markdown 使用同一时间戳前缀，便于成对查找。"""
    report = _report()
    json_path = write_json(report, tmp_path)
    md_path = write_markdown(report, tmp_path)

    assert json_path.stem == md_path.stem
    assert json_path.name.startswith("eval-")
    assert json_path.suffix == ".json"
    assert md_path.suffix == ".md"

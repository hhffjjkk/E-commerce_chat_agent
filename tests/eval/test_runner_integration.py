"""runner 集成测试：Mock 全流程、跳过策略、真实模式校验与 Token 字段。"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 app/runner 之前清掉外部密钥，保证默认走 Mock 且 Langfuse no-op
for _key in (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
):
    os.environ.pop(_key, None)

from agent_system.agent import LLMClient, _usage_tokens  # noqa: E402
from agent_system.eval import runner  # noqa: E402
from agent_system.eval.collector import EvalCollector  # noqa: E402
from agent_system.eval.langfuse_eval import upload_dataset, upload_scores  # noqa: E402
from agent_system.eval.task_schema import load_tasks  # noqa: E402


def _write_tasks(path: Path) -> None:
    """写入一份 5 条的精简任务集，覆盖通过、缓存与跳过。"""
    tasks = [
        {
            "id": "mock-product",
            "category": "route_rule_hit",
            "session_id": "eval-mock-product",
            "messages": ["显示器多少钱"],
            "expect": {"route_agent": "pre_sales", "contains": ["1899"]},
            "notes": "售前价格查询应通过",
        },
        {
            "id": "mock-order",
            "category": "route_rule_hit",
            "session_id": "eval-mock-order",
            "messages": ["查订单 20261101001"],
            "expect": {"route_agent": "order", "contains": ["已发货"]},
            "notes": "订单状态查询应通过",
        },
        {
            "id": "mock-cache-hit",
            "category": "cache_hit",
            "session_id": "eval-mock-cache-hit",
            "messages": ["护眼台灯多少钱", "护眼台灯多少钱"],
            "expect": {
                "route_agent": "pre_sales",
                "cache_hit": True,
                "contains": ["129"],
            },
            "notes": "第二次应命中缓存",
        },
        {
            "id": "mock-cache-miss",
            "category": "cache_miss_not_cached",
            "session_id": "eval-mock-cache-miss",
            "messages": ["我要退款", "我要退款"],
            "expect": {"route_agent": "after_sales", "cache_hit": False},
            "notes": "售后消息不应命中缓存",
        },
        {
            "id": "mock-skip",
            "category": "route_llm_fallback",
            "session_id": "eval-mock-skip",
            "messages": ["帮我看看这个怎么样"],
            "expect": {"route_agent": "pre_sales"},
            "only_real_llm": True,
            "notes": "仅真实模式运行",
        },
    ]
    path.write_text(
        "\n".join(json.dumps(task, ensure_ascii=False) for task in tasks) + "\n",
        encoding="utf-8",
    )


def test_mock_run_produces_reports_and_skips_real_only(tmp_path):
    """Mock 全流程：生成报告、通过率达标、跳过任务被记录、缓存命中 >= 1。"""
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path)
    reports_dir = tmp_path / "reports"

    report = runner.run(tasks_path, mode="mock", reports_dir=reports_dir)

    assert report.mode == "mock"
    assert report.executed == 4
    assert report.skipped == 1
    assert report.pass_rate >= 0.6
    assert report.cache_hit_count >= 1
    assert [item["task_id"] for item in report.skipped_tasks] == ["mock-skip"]
    assert all(item.notes for item in report.results)

    assert list(reports_dir.glob("eval-*-mock.json"))
    assert list(reports_dir.glob("eval-*-mock.md"))


def test_real_mode_without_api_key_raises(tmp_path, monkeypatch):
    """强制 real 模式但无 API Key 时立即报错，而不是静默降级。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tasks_path = tmp_path / "tasks.jsonl"
    _write_tasks(tasks_path)

    with pytest.raises(RuntimeError, match="mode=real"):
        runner.run(tasks_path, mode="real", reports_dir=tmp_path / "reports")


def test_langfuse_upload_is_noop_when_disabled(monkeypatch, tmp_path):
    """未启用 Langfuse 时上传函数直接返回 0，不抛异常。"""
    monkeypatch.setattr(
        "agent_system.eval.langfuse_eval.is_enabled", lambda: False
    )
    tasks = load_tasks(ROOT / "evals" / "tasks.jsonl")
    report = SimpleNamespace(results=[])

    assert upload_dataset(tasks) == 0
    assert upload_scores(report, {}) == 0


class _Completions:
    """伪 chat.completions：返回带 usage 的响应。"""

    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(usage=self.usage, choices=[])


def test_call_llm_records_prompt_and_completion_tokens():
    """真实调用路径的 llm_call(phase=end) 必须带 token 字段，缺失时给 0。"""
    client = object.__new__(LLMClient)
    client.model = "fake-model"
    completions = _Completions(SimpleNamespace(prompt_tokens=123, completion_tokens=45))
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    collector = EvalCollector()
    with collector.install():
        collector.start_task("tokens")
        client._call_llm(model="fake-model", messages=[])
        snapshot = collector.stop_task()

    assert snapshot["llm_calls"] == 1
    assert snapshot["prompt_tokens"] == 123
    assert snapshot["completion_tokens"] == 45

    # usage 缺失（Mock 模式或供应商未返回）时必须降级为 0，而不是抛异常
    assert _usage_tokens(None) == (0, 0)
    assert _usage_tokens(SimpleNamespace(usage=None)) == (0, 0)

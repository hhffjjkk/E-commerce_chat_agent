"""collect_baseline 脚本契约测试：不调用真实 LLM。"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.load_runner import _EvalEnvironment  # noqa: E402
from agent_system.eval.metrics import compute_report  # noqa: E402
from scripts import collect_baseline  # noqa: E402


def _stub_report():
    """构造一个不含任务的 EvalReport 桩，避免真正执行评测。"""
    return compute_report(
        mode="mock",
        seed=42,
        started_at=datetime.now().isoformat(timespec="seconds"),
        finished_at=datetime.now().isoformat(timespec="seconds"),
        results=[],
        skipped_tasks=[],
        cache_hit_count=0,
        cache_miss_count=0,
    )


@pytest.fixture
def stub_run(monkeypatch):
    """把 run 替换为固定报告，并把限流开关替换为 no-op。"""
    import agent_system.eval.load_runner as load_runner

    calls = []

    def fake_run(tasks_path, *, mode="auto", reports_dir=None):
        calls.append({"tasks_path": str(tasks_path), "mode": mode})
        return _stub_report()

    monkeypatch.setattr(collect_baseline, "run", fake_run)
    monkeypatch.setattr(load_runner, "_disable_rate_limit", lambda: [])
    monkeypatch.setattr(load_runner, "_restore_rate_limit", lambda restored: None)
    return calls


def test_collect_writes_config_snapshot(tmp_path, stub_run):
    """no_nmsg_cache 产出 JSON 必须带与变体一致的 config_snapshot。"""
    path = collect_baseline.collect(
        "no_nmsg_cache", out_dir=tmp_path, mode="mock"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    snapshot = payload["config_snapshot"]

    assert path.name.startswith("baseline-no_nmsg_cache-")
    assert snapshot["variant"] == "no_nmsg_cache"
    assert snapshot["requested_env"] == {"NMSG_CACHE_ENABLED": "false"}
    assert snapshot["effective"]["nmsg_cache_enabled"] is False
    assert snapshot["effective"]["mode"] == "mock"
    assert snapshot["effective"]["llm_backend"] == "MockLLM"
    assert "code_version" in snapshot
    assert payload["executed"] == 0
    assert stub_run[0]["mode"] == "mock"
    # 运行结束后环境变量必须恢复，避免污染后续变体
    assert os.environ.get("NMSG_CACHE_ENABLED") is None


def test_cli_parses_multiple_variants(tmp_path, stub_run, monkeypatch):
    """CLI 接受多个变体并按顺序执行。"""
    monkeypatch.setattr(
        collect_baseline,
        "collect",
        lambda variant, **kwargs: tmp_path / f"{variant}.json",
    )

    code = collect_baseline.main(
        ["--variant", "no_multi_agent", "no_nmsg_cache", "--mode", "mock"]
    )

    assert code == 0
    assert stub_run == []


def test_unknown_variant_raises_value_error():
    """未知变体必须报错并列出可选值。"""
    with pytest.raises(ValueError, match="未知变体"):
        collect_baseline.collect("magic", out_dir=Path("."))


def test_unknown_variant_is_rejected_by_cli():
    """argparse 的 choices 会拒绝未知变体。"""
    with pytest.raises(SystemExit):
        collect_baseline.main(["--variant", "magic"])


def test_no_multi_agent_restores_environment():
    """no_multi_agent 不设置环境变量，退出后 EvalEnvironment 与选择函数均恢复。"""
    from agent_system import app as app_module
    from agent_system.eval import runner as runner_module

    original_select = app_module.select_profile
    original_env = runner_module._EvalEnvironment

    with collect_baseline.apply_variant("no_multi_agent"):
        assert app_module.select_profile is not original_select

    assert app_module.select_profile is original_select
    assert runner_module._EvalEnvironment is _EvalEnvironment
    assert runner_module._EvalEnvironment is original_env

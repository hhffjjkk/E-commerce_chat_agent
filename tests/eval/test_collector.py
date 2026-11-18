"""事件采集器测试：hook 生效、指标归集与恢复。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system import tools as tools_module  # noqa: E402
from agent_system.eval.collector import EvalCollector  # noqa: E402
from agent_system.obs import events as events_module  # noqa: E402


def _emit_sample_events() -> None:
    """触发一组覆盖主要派生指标的事件。"""
    events_module.log_event(
        "route_selected",
        agent="order",
        display_name="订单与物流",
        strategy="rule",
        reason="below_threshold",
        confidence=0.5,
        latency_ms=1.2,
    )
    events_module.log_event("llm_call", phase="start", model="fake-model", stream=False)
    events_module.log_event(
        "llm_call",
        phase="end",
        model="fake-model",
        stream=False,
        ok=True,
        latency_ms=12.5,
        prompt_tokens=100,
        completion_tokens=20,
    )
    events_module.log_event(
        "tool_call", tool="query_order", args_keys=["order_id"], ok=True, latency_ms=2.0
    )
    events_module.log_event("cache_hit", layer="nmsg")
    events_module.log_event("cache_miss", layer="nmsg", reason="not_found")
    events_module.log_event("cache_write", layer="nmsg", ttl_seconds=60)
    events_module.log_event("cache_write_skipped", layer="nmsg", reason="not_cacheable")
    events_module.log_event("confirmation_requested", action_id="a1", tool="apply_refund")
    events_module.log_event("error_raised", code="tool_failed", retryable=True)


def test_collector_derives_metrics_and_restores_hook():
    """hook 内的模拟事件应被正确折叠，退出后 log_event 恢复原函数。"""
    original = events_module.log_event
    collector = EvalCollector()

    with collector.install():
        # 直接调用 events 模块的 log_event：install 后它应已被替换
        assert events_module.log_event is not original
        assert tools_module.log_event is not original

        collector.start_task("task-a")
        _emit_sample_events()
        snapshot = collector.stop_task()

    assert events_module.log_event is original
    assert tools_module.log_event is original

    assert snapshot["task_id"] == "task-a"
    assert snapshot["route"]["agent"] == "order"
    assert snapshot["route"]["reason"] == "below_threshold"
    assert snapshot["llm_calls"] == 1
    assert snapshot["prompt_tokens"] == 100
    assert snapshot["completion_tokens"] == 20
    assert snapshot["llm_latency_ms"] == 12.5
    assert snapshot["tools_used"] == ["query_order"]
    assert snapshot["tool_ok"] == 1
    assert snapshot["tool_failed"] == 0
    assert snapshot["cache_hit"] == 1
    assert snapshot["cache_miss"] == 1
    assert snapshot["cache_write"] == 1
    assert snapshot["cache_write_skipped"] == 1
    assert snapshot["cache_write_skipped_reasons"] == ["not_cacheable"]
    assert snapshot["confirmation"]["confirmation_requested"] == 1
    assert snapshot["errors"] == ["tool_failed"]


def test_orphan_events_are_ignored():
    """start_task 之前与 stop_task 之后的事件不计入任何任务。"""
    collector = EvalCollector()
    with collector.install():
        events_module.log_event("route_selected", agent="pre_sales", strategy="fallback")
        collector.start_task("task-b")
        events_module.log_event("tool_call", tool="query_product", ok=True, latency_ms=1.0)
        snapshot = collector.stop_task()
        events_module.log_event("cache_hit", layer="nmsg")

    assert snapshot["route"] is None
    assert snapshot["tools_used"] == ["query_product"]
    assert collector.snapshot()["cache_hit"] == 0


def test_tasks_do_not_pollute_each_other():
    """多任务切换时快照互不污染。"""
    collector = EvalCollector()
    with collector.install():
        collector.start_task("task-c")
        events_module.log_event("route_selected", agent="order", strategy="rule")
        first = collector.stop_task()

        collector.start_task("task-d")
        events_module.log_event("cache_hit", layer="nmsg")
        second = collector.stop_task()

    assert first["route"]["agent"] == "order"
    assert first["cache_hit"] == 0
    assert second["route"] is None
    assert second["cache_hit"] == 1
    assert collector.task_snapshot("task-c")["route"]["agent"] == "order"

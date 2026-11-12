"""确定性 code evaluator 测试：成功率、错误率与 score 上报。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.code_evaluators import (  # noqa: E402
    error_rate,
    evaluate_trajectory,
    report_scores,
    tool_success_rate,
)


def _tool(ok: bool) -> dict:
    """构造一条 tool_call 事件。"""
    return {"event": "tool_call", "tool": "query_order", "ok": ok, "latency_ms": 1.0}


def test_tool_success_rate_two_of_three():
    """3 次工具调用中 2 次成功 -> 2/3。"""
    events = [_tool(True), _tool(True), _tool(False)]
    score = tool_success_rate(events)
    assert score == {
        "name": "tool_success_rate",
        "value": round(2 / 3, 4),
        "data_type": "NUMERIC",
    }


def test_tool_success_rate_without_tool_calls():
    """没有工具调用时不做评分，返回 None。"""
    assert tool_success_rate([{"event": "request_start"}]) is None


def test_error_rate_detects_error_events():
    """出现 error_raised 或 request_error 时错误率为 1，否则为 0。"""
    assert error_rate([{"event": "request_start"}, {"event": "error_raised"}])["value"] == 1
    assert error_rate([{"event": "request_error"}])["value"] == 1
    assert error_rate([{"event": "request_start"}, _tool(True)])["value"] == 0


def test_evaluate_trajectory_combines_scores():
    """完整轨迹同时产出工具成功率与错误率两个分数。"""
    events = [_tool(True), _tool(False), {"event": "error_raised"}]
    scores = evaluate_trajectory(events)
    assert [score["name"] for score in scores] == ["tool_success_rate", "error_rate"]
    assert scores[0]["value"] == 0.5
    assert scores[1]["value"] == 1


def test_evaluator_is_deterministic_without_llm():
    """同一轨迹重复评估结果一致，说明不依赖外部 LLM/网络。"""
    events = [_tool(True), _tool(True), _tool(False)]
    assert evaluate_trajectory(events) == evaluate_trajectory(events)


class _FakeScoreClient:
    """记录 create_score 调用的 fake 客户端。"""

    def __init__(self):
        self.calls = []

    def create_score(self, **kwargs):
        """记录一次评分上报。"""
        self.calls.append(kwargs)


def test_report_scores_uses_create_score():
    """evaluator 分数可以通过 v3 的 create_score 上报。"""
    client = _FakeScoreClient()
    scores = evaluate_trajectory([_tool(True)])
    count = report_scores(client, "trace-abc", scores)

    assert count == len(scores)
    assert client.calls[0]["trace_id"] == "trace-abc"
    assert client.calls[0]["name"] == "tool_success_rate"
    assert client.calls[0]["data_type"] == "NUMERIC"

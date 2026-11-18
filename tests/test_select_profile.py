"""select_profile 三级决策单测：Router、pending 粘滞、防御性回退。"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.harness import selection as selection_module  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import get_guardrails, reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import get_pending_store, reset_pending_store  # noqa: E402
from agent_system.harness.router import RouteDecision, RoutePolicy, RouteStrategy, reset_router  # noqa: E402
from agent_system.harness.selection import select_profile  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """注册工具/Profile，并重置 Router、Guardrails 与 pending。"""
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    reset_guardrails()
    reset_pending_store()
    yield
    reset_router()
    reset_guardrails()
    reset_pending_store()
    reset_profile_registry()
    register_builtin_profiles()


@pytest.fixture
def events(monkeypatch):
    """收集 selection 模块事件。"""
    captured = []
    monkeypatch.setattr(
        selection_module,
        "log_event",
        lambda event, **fields: captured.append({"event": event, **fields}),
    )
    return captured


def test_router_hit_returns_decision(events):
    """无 pending 且规则命中：reason=router，decision 非 None。"""
    profile, decision, reason = select_profile("s1", "我的订单到哪了")

    assert profile.name == "order"
    assert decision is not None
    assert decision.agent == "order"
    assert reason == "router"
    selected = [e for e in events if e["event"] == "route_selected"]
    assert len(selected) == 1
    assert selected[0]["agent"] == "order"
    assert selected[0]["strategy"] == decision.strategy.value


def test_pending_overrides_to_after_sales(events):
    """存在 pending 时强制 after_sales，decision=None。"""
    get_guardrails().intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "s-pending"
    )

    profile, decision, reason = select_profile("s-pending", "确认")
    assert profile.name == "after_sales"
    assert decision is None
    assert reason == "pending_override"

    selected = [e for e in events if e["event"] == "route_selected"]
    assert selected[0]["strategy"] == "pending_override"
    assert selected[0]["confidence"] == 1.0


def test_expired_pending_does_not_stick(events):
    """已过期的 pending 不粘滞，回到 Router。"""
    payload = get_guardrails().intercept_write(
        "cancel_order", {"order_id": "20261103001"}, "s-expired"
    )
    pending = get_pending_store().get(payload["action_id"])
    # pending 时间戳是墙钟
    pending.expires_at = time.time() - 1

    profile, decision, reason = select_profile("s-expired", "你好")
    assert reason == "router"
    assert decision is not None
    assert profile.name == "pre_sales"


def test_profile_missing_falls_back(monkeypatch, events):
    """Router 返回未注册 agent 时回退默认 Profile。"""
    fake_router = SimpleNamespace(
        policy=RoutePolicy(),
        route=lambda message: RouteDecision(
            agent="missing",
            confidence=0.9,
            reason="test",
            strategy=RouteStrategy.RULE,
        ),
    )
    monkeypatch.setattr(selection_module, "get_router", lambda: fake_router)

    profile, decision, reason = select_profile("s-missing", "x")
    assert profile.name == "pre_sales"
    assert reason == "profile_missing"
    assert decision is not None


def test_router_exception_falls_back(monkeypatch, events):
    """Router 抛异常时回退默认 Profile，并记录失败事件。"""

    def boom(message):
        raise RuntimeError("router down")

    fake_router = SimpleNamespace(policy=RoutePolicy(), route=boom)
    monkeypatch.setattr(selection_module, "get_router", lambda: fake_router)

    profile, decision, reason = select_profile("s-error", "x")
    assert profile.name == "pre_sales"
    assert decision is None
    assert reason == "router_exception"

    failed = [e for e in events if e["event"] == "route_selection_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "router_exception"
    assert failed[0]["error_type"] == "RuntimeError"

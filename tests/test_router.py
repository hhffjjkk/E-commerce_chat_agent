"""Router 单测：三级降级、阈值、默认 Profile、事件与单例。"""

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.harness import router as router_module  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.router import (  # noqa: E402
    RoutePolicy,
    RouteStrategy,
    Router,
    get_router,
    reset_router,
)


@pytest.fixture(autouse=True)
def profiles_ready():
    """注册工具与内置 Profile，并重置 Router 单例。"""
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    yield
    reset_router()
    reset_profile_registry()
    # 恢复内置 Profile，避免影响后续依赖 /chat 的测试
    register_builtin_profiles()


@pytest.fixture
def events(monkeypatch):
    """收集 route_* 事件。"""
    captured = []
    monkeypatch.setattr(
        router_module,
        "log_event",
        lambda event, **fields: captured.append({"event": event, **fields}),
    )
    return captured


class FakeCompletions:
    """记录调用参数并按配置返回/抛异常。"""

    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


class FakeClient:
    """最小的 chat.completions 客户端。"""

    def __init__(self, content=None, exc=None):
        self._completions = FakeCompletions(content, exc)
        self.chat = SimpleNamespace(completions=self._completions)

    @property
    def calls(self):
        return self._completions.calls


def _llm_json(agent: str, confidence: float, reason: str = "test") -> str:
    """构造合法路由 JSON。"""
    return json.dumps({"agent": agent, "confidence": confidence, "reason": reason})


def _policy(**overrides) -> RoutePolicy:
    """构造合法策略。"""
    params = {
        "strategy": "rule_first",
        "confidence_threshold": 0.55,
        "default_profile": "pre_sales",
        "llm_enabled": True,
        "llm_timeout_seconds": 5.0,
    }
    params.update(overrides)
    return replace(RoutePolicy(), **params)


def test_policy_defaults_and_validation():
    """默认策略合法；非法枚举/阈值/超时被拒绝。"""
    policy = RoutePolicy()
    policy.validate()
    assert policy.strategy == "rule_first"
    assert policy.confidence_threshold == 0.55
    assert policy.default_profile == "pre_sales"

    with pytest.raises(ValueError):
        _policy(strategy="unknown").validate()
    with pytest.raises(ValueError):
        _policy(confidence_threshold=1.5).validate()
    with pytest.raises(ValueError):
        _policy(llm_timeout_seconds=0).validate()


def test_policy_from_env(monkeypatch):
    """合法环境变量生效；非法值回退并记录变量名。"""
    monkeypatch.setenv("ROUTER_STRATEGY", "llm_only")
    monkeypatch.setenv("ROUTER_CONFIDENCE_THRESHOLD", "0.7")
    monkeypatch.setenv("ROUTER_DEFAULT_PROFILE", "order")
    monkeypatch.setenv("ROUTER_LLM_ENABLED", "false")
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_SECONDS", "2.5")

    policy, invalid = RoutePolicy.from_env()
    assert policy.strategy == "llm_only"
    assert policy.confidence_threshold == 0.7
    assert policy.default_profile == "order"
    assert policy.llm_enabled is False
    assert policy.llm_timeout_seconds == 2.5
    assert invalid == []

    monkeypatch.setenv("ROUTER_STRATEGY", "bad")
    monkeypatch.setenv("ROUTER_CONFIDENCE_THRESHOLD", "2")
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_SECONDS", "0")
    fallback, invalid = RoutePolicy.from_env()
    assert fallback.strategy == "rule_first"
    assert fallback.confidence_threshold == 0.55
    assert fallback.llm_timeout_seconds == 5.0
    assert set(invalid) == {
        "ROUTER_STRATEGY",
        "ROUTER_CONFIDENCE_THRESHOLD",
        "ROUTER_LLM_TIMEOUT_SECONDS",
    }


def test_policy_id_is_stable_and_reactive():
    """相同字段 ID 稳定，字段变化 ID 变化。"""
    assert RoutePolicy().policy_id() == RoutePolicy().policy_id()
    assert RoutePolicy().policy_id() != _policy(strategy="rule_only").policy_id()


def test_no_profiles_falls_back(events):
    """没有 Profile 时直接返回默认 Profile。"""
    reset_profile_registry()
    decision = Router(_policy(), llm_client=None).route("你好")

    assert decision.strategy == RouteStrategy.FALLBACK
    assert decision.agent == "pre_sales"
    assert decision.reason == "no_profiles"
    assert decision.confidence == 0.0
    assert len([e for e in events if e["event"] == "route_decided"]) == 1


def test_rule_only_hit_and_miss(events):
    """rule_only：命中走 RULE，未命中走 FALLBACK。"""
    router = Router(_policy(strategy="rule_only"), llm_client=None)

    hit = router.route("我要退款")
    assert hit.strategy == RouteStrategy.RULE
    assert hit.agent == "after_sales"

    miss = router.route("今天天气不错")
    assert miss.strategy == RouteStrategy.FALLBACK
    assert miss.agent == "pre_sales"
    assert miss.reason == "no_rule_hit"


def test_llm_only_without_llm_falls_back(events):
    """llm_only 且 LLM 不可用：返回默认 Profile。"""
    decision = Router(_policy(strategy="llm_only"), llm_client=None).route("我要退款")
    assert decision.strategy == RouteStrategy.FALLBACK
    assert decision.reason == "no_llm"
    assert decision.agent == "pre_sales"


def test_rule_first_high_confidence_skips_llm(events):
    """规则高置信时直接返回，不调用 LLM。"""
    client = FakeClient(content=_llm_json("order", 0.9))
    router = Router(_policy(), llm_client=client)

    decision = router.route("我要退款退货")  # 命中两个售后关键词
    assert decision.strategy == RouteStrategy.RULE
    assert decision.agent == "after_sales"
    assert decision.llm_used is False
    assert client.calls == []


def test_rule_first_low_confidence_uses_llm(events):
    """规则低置信、LLM 高置信：采用 LLM。"""
    client = FakeClient(content=_llm_json("after_sales", 0.9))
    router = Router(_policy(), llm_client=client)

    decision = router.route("我要退款")  # 规则 0.5 < 0.55
    assert decision.strategy == RouteStrategy.LLM
    assert decision.agent == "after_sales"
    assert decision.llm_used is True
    assert len(client.calls) == 1


def test_rule_first_llm_low_confidence_keeps_rule(events):
    """LLM 低置信时保留规则结果并标注 below_threshold。"""
    client = FakeClient(content=_llm_json("pre_sales", 0.2))
    router = Router(_policy(), llm_client=client)

    decision = router.route("我要退款")
    assert decision.strategy == RouteStrategy.RULE
    assert decision.agent == "after_sales"
    assert decision.reason == "below_threshold"


def test_llm_first_success_and_fallback(events):
    """llm_first：LLM 高置信成功；失败时回退规则；规则也无则默认。"""
    success = Router(
        _policy(strategy="llm_first"),
        llm_client=FakeClient(content=_llm_json("order", 0.9)),
    ).route("今天天气不错")
    assert success.strategy == RouteStrategy.LLM
    assert success.agent == "order"

    fallback_rule = Router(
        _policy(strategy="llm_first"),
        llm_client=FakeClient(exc=ValueError("boom")),
    ).route("我要退款")
    assert fallback_rule.strategy == RouteStrategy.RULE
    assert fallback_rule.agent == "after_sales"

    fallback_default = Router(
        _policy(strategy="llm_first"),
        llm_client=FakeClient(exc=ValueError("boom")),
    ).route("今天天气不错")
    assert fallback_default.strategy == RouteStrategy.FALLBACK
    assert fallback_default.reason == "llm_exception"


def test_invalid_default_profile_uses_first_profile(events):
    """配置的默认 Profile 不存在时，回退到注册表第一个并记录原因。"""
    decision = Router(
        _policy(default_profile="missing", llm_enabled=False),
        llm_client=None,
    ).route("今天天气不错")

    assert decision.agent == "pre_sales"
    assert decision.reason == "invalid_default_profile"
    assert decision.strategy == RouteStrategy.FALLBACK


def test_route_decided_event_has_policy_reason_only(events):
    """每次 route 恰好一条 route_decided，reason 不含用户消息。"""
    message = "我的订单到哪了"
    Router(_policy(strategy="rule_only"), llm_client=None).route(message)

    decided = [e for e in events if e["event"] == "route_decided"]
    assert len(decided) == 1
    assert decided[0]["agent"] == "order"
    assert decided[0]["strategy"] == "rule"
    assert message not in json.dumps(decided[0], ensure_ascii=False)


def test_singleton_reads_env_after_reset(monkeypatch):
    """单例稳定；reset 后按新环境变量重建。"""
    first = get_router()
    assert get_router() is first

    reset_router()
    monkeypatch.setenv("ROUTER_STRATEGY", "rule_only")
    second = get_router()
    assert second is not first
    assert second.policy.strategy == "rule_only"

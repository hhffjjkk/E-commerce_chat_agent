"""LLM 路由单测：严格 JSON 解析、容错、超时与轻量调用参数。"""

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
from agent_system.harness.agent_profile import get_profile_registry, reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.router import RoutePolicy, Router  # noqa: E402
from agent_system.harness.router_prompts import (  # noqa: E402
    build_router_messages,
    parse_router_response,
    render_profiles_block,
)


class APITimeoutError(Exception):
    """名称包含 Timeout，用于验证超时分类。"""


class FakeCompletions:
    """记录参数并返回预设文本或抛异常。"""

    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeClient:
    """最小 chat.completions 客户端。"""

    def __init__(self, content=None, exc=None):
        self._completions = FakeCompletions(content, exc)
        self.chat = SimpleNamespace(completions=self._completions)

    @property
    def calls(self):
        return self._completions.calls


@pytest.fixture(autouse=True)
def profiles_ready():
    """注册工具与内置 Profile。"""
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    yield
    reset_profile_registry()
    # 恢复内置 Profile，保证后续 /chat 测试有可用路由目标
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


def _router(client, **overrides) -> Router:
    """构造 llm_only 策略的 Router。"""
    params = {"strategy": "llm_only", "confidence_threshold": 0.55}
    params.update(overrides)
    return Router(replace(RoutePolicy(), **params), llm_client=client)


def test_parse_valid_json():
    """合法 JSON 正确解析。"""
    agent, confidence, reason = parse_router_response(
        '{"agent": "order", "confidence": 0.8, "reason": "查物流"}',
        {"order", "pre_sales"},
    )
    assert (agent, confidence, reason) == ("order", 0.8, "查物流")


def test_parse_fenced_json_and_missing_fields():
    """去围栏；缺失 confidence 给 0.5，缺失 reason 给空串。"""
    raw = '```json\n{"agent": "order"}\n```'
    assert parse_router_response(raw, {"order"}) == ("order", 0.5, "")


def test_parse_clamps_confidence_and_rejects_invalid():
    """confidence 越界夹到 [0,1]；非法 agent/JSON 返回错误标签。"""
    assert parse_router_response(
        '{"agent": "order", "confidence": 9}', {"order"}
    )[1] == 1.0
    assert parse_router_response(
        '{"agent": "order", "confidence": -3}', {"order"}
    )[1] == 0.0
    assert parse_router_response(
        '{"agent": "unknown", "confidence": 0.9}', {"order"}
    ) == (None, 0.0, "invalid_agent")
    assert parse_router_response("not json", {"order"}) == (None, 0.0, "parse_error")


def test_profile_block_and_messages():
    """候选块每行一个 Profile，messages 含 system/user。"""
    profiles = get_profile_registry().list()
    block = render_profiles_block(profiles)
    assert block.splitlines()[0].startswith("- pre_sales:")
    messages = build_router_messages("我的订单", profiles)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "我的订单"


def test_llm_success_records_event(events):
    """合法响应：error_tag=ok，记录 route_llm_call。"""
    client = FakeClient(
        content=json.dumps({"agent": "order", "confidence": 0.9, "reason": "物流"})
    )
    router = _router(client)
    agent, confidence, reason, tag = router._route_with_llm(
        "我的订单到哪了", get_profile_registry().list()
    )

    assert (agent, confidence, tag) == ("order", 0.9, "ok")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert "tools" not in call
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 200

    llm_events = [e for e in events if e["event"] == "route_llm_call"]
    assert len(llm_events) == 1
    assert llm_events[0]["error_tag"] == "ok"
    assert llm_events[0]["raw_len"] > 0


def test_llm_parse_error_and_invalid_agent(events):
    """非法 JSON/invalid agent 返回对应 error_tag。"""
    parse_router = _router(FakeClient(content="oops"))
    assert parse_router._route_with_llm("x", get_profile_registry().list())[3] == "parse_error"

    invalid_router = _router(FakeClient(content='{"agent": "nope"}'))
    assert (
        invalid_router._route_with_llm("x", get_profile_registry().list())[3]
        == "invalid_agent"
    )


def test_llm_exception_and_timeout_tags(events):
    """普通异常归为 exception，超时类归为 timeout。"""
    exception_router = _router(FakeClient(exc=ValueError("boom")))
    assert (
        exception_router._route_with_llm("x", get_profile_registry().list())[3]
        == "exception"
    )

    timeout_router = _router(FakeClient(exc=APITimeoutError("slow")))
    assert (
        timeout_router._route_with_llm("x", get_profile_registry().list())[3]
        == "timeout"
    )

    tags = [e["error_tag"] for e in events if e["event"] == "route_llm_call"]
    assert tags == ["exception", "timeout"]


def test_llm_no_client_tag():
    """没有注入客户端时返回 no_llm，不记录调用事件。"""
    router = Router(replace(RoutePolicy(), strategy="llm_only"), llm_client=None)
    router.llm_client = None  # 防止构造时延迟实例化成功
    assert router._route_with_llm("x", get_profile_registry().list())[3] == "no_llm"

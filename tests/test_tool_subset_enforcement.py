"""工具子集强制测试：不同 Profile 下发不同 tools，禁用工具时降级。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import PRE_SALES_PROFILE, register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import reset_pending_store  # noqa: E402
from agent_system.harness.router import reset_router  # noqa: E402
from agent_system.harness.tool_registry import get_registry  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """收集结构化日志。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """挂载 JSON handler。"""
    logger = get_logger()
    handler = ListHandler()
    handler.setFormatter(JSONFormatter("test-service"))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    """隔离会话库、Router、Guardrails 与 Profile。"""
    from agent_system.memory import SessionStore

    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    reset_router()
    reset_guardrails()
    reset_pending_store()
    original_store = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original_store
        reset_router()
        reset_guardrails()
        reset_pending_store()
        reset_profile_registry()
        register_builtin_profiles()


class FakeCompletions:
    """记录 create 参数并返回无 tool_calls 的最终回复。"""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _bot():
    """构造 fake LLMClient。"""
    completions = FakeCompletions()
    bot = object.__new__(LLMClient)
    bot.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    bot.model = "fake-model"
    bot._interrupt_probe = None
    bot._last_loop = None
    return bot, completions


def _post(message, request_id):
    """调用 /chat。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/chat",
                json={"session_id": "subset", "message": message},
                headers={"X-Request-ID": request_id},
            )

    return asyncio.run(run())


def _tool_names(call) -> set[str]:
    """提取一次 create 调用下发的工具名。"""
    return {item["function"]["name"] for item in call["tools"]}


def test_pre_sales_tools_subset(monkeypatch):
    """售前消息只下发商品/FAQ 工具，不含订单与写操作。"""
    bot, completions = _bot()
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("这个商品多少钱", "subset-pre-0001")
    assert response.status_code == 200

    names = _tool_names(completions.calls[0])
    assert names == {"query_product", "search_faq"}
    assert "apply_refund" not in names
    assert "cancel_order" not in names
    assert "query_order" not in names


def test_after_sales_tools_subset(monkeypatch):
    """售后消息下发写工具与确认工具，不含商品/物流查询。"""
    bot, completions = _bot()
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("我要退款", "subset-after-01")
    assert response.status_code == 200

    names = _tool_names(completions.calls[0])
    assert {"apply_refund", "confirm_action", "query_order"} <= names
    assert "query_product" not in names
    assert "query_logistics" not in names


def test_order_tools_subset(monkeypatch):
    """订单消息只下发订单/物流查询。"""
    bot, completions = _bot()
    monkeypatch.setattr(app_module, "bot", bot)

    response = _post("我的订单到哪了", "subset-order-01")
    assert response.status_code == 200

    names = _tool_names(completions.calls[0])
    assert names == {"query_order", "query_logistics"}
    assert not names & {"apply_refund", "cancel_order"}


def test_disabled_tool_is_filtered_and_observable(monkeypatch, captured_logs):
    """Profile 工具被 disable 时过滤并记录 profile_tool_missing，请求仍成功。"""
    registry = get_registry()
    registry.disable("query_product")
    try:
        bot, completions = _bot()
        monkeypatch.setattr(app_module, "bot", bot)
        response = _post("这个商品多少钱", "subset-disabled")
    finally:
        registry.enable("query_product")

    assert response.status_code == 200
    names = _tool_names(completions.calls[0])
    assert names == {"search_faq"}

    events = [json.loads(captured_logs.format(r)) for r in captured_logs.records]
    missing = [e for e in events if e["event"] == "profile_tool_missing"]
    assert missing
    assert missing[-1]["profile"] == "pre_sales"
    assert "query_product" in missing[-1]["missing"]


def test_profile_none_keeps_full_tools():
    """profile=None 时保持全量 8 条工具（向后兼容）。"""
    bot, completions = _bot()
    messages = LLMClient._run_tool_loop(bot, [{"role": "user", "content": "hi"}])
    assert messages
    names = _tool_names(completions.calls[0])
    assert len(names) == 8
    assert names == {item["function"]["name"] for item in bot.TOOLS}


def test_tools_property_still_full():
    """LLMClient.TOOLS property 仍返回全量 8 条。"""
    bot, _ = _bot()
    assert len(bot.TOOLS) == 8
    assert len(PRE_SALES_PROFILE.tool_names) == 2

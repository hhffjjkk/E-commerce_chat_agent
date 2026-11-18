"""回归测试：ToolRegistry.call 改用 arguments 字典，修复阶段三的参数名冲突。

背景：
    旧签名 `call(name, **kwargs)` 在工具入参也叫 `name`（query_product）时，
    Python 会把关键字参数绑定到 Registry 的形参 `name`，抛
    “got multiple values for argument 'name'”，导致商品查询 500。

覆盖：
    - query_product 关键路径；
    - 8 个内置工具统一用 arguments 字典调用；
    - 未注册 / 已禁用工具的异常口径不变；
    - arguments=None 与 {} 等价；
    - LLMClient._call_tool 调用点；
    - /chat 与 /chat/stream 端到端不再出现 internal_error。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system import tools  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.errors.exceptions import ToolExecutionError  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.guardrails import reset_guardrails  # noqa: E402
from agent_system.harness.pending_store import reset_pending_store  # noqa: E402
from agent_system.harness.router import reset_router  # noqa: E402
from agent_system.harness.tool_registry import ToolRegistry, ToolSpec, get_registry  # noqa: E402
from agent_system.obs.request_id import reset_session_id, set_session_id  # noqa: E402


@pytest.fixture
def registry():
    """每个用例使用独立注册表，避免污染全局单例。"""
    table = ToolRegistry()
    register_all(table)
    return table


@pytest.fixture(autouse=True)
def clean_state(tmp_path):
    """隔离会话库、Profile、Router 与确认相关的全局状态。"""
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


class _RegistryProductBot:
    """模拟 LLM 工具循环：按修复后的 arguments 约定执行 query_product。

    说明:
        真实 LLM 由 SDK 返回 tool_calls，再由 `_call_tool` 分发；
        这里直接调用 Registry，等价覆盖“HTTP → Agent → Registry”的链路。
    """

    def reply(self, message, history=None, *, profile=None):
        data = get_registry().call("query_product", arguments={"name": "极光笔记本"})
        return f"{data['name']} 价格 ¥{data['price']}"

    def reply_stream(self, message, history=None, *, profile=None):
        yield self.reply(message, history)


def _patch_write_source(monkeypatch):
    """只替换写操作的最终执行，避免测试改动 data/orders.json。

    说明:
        读工具继续走真实 JsonDataSource，确保 8 个工具的参数构造都来自演示数据；
        仅把 apply_refund / cancel_order 的执行结果换成固定值。
    """
    monkeypatch.setattr(
        tools._source,
        "apply_refund",
        lambda order_id, reason: {"found": True, "success": True, "message": "退款已提交"},
    )
    monkeypatch.setattr(
        tools._source,
        "cancel_order",
        lambda order_id: {"found": True, "success": True, "message": "订单已取消"},
    )


def _post(path: str, body: dict, request_id: str):
    """通过 ASGITransport 调用 HTTP 端点（不依赖真实网络）。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, json=body, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def test_query_product_with_name_keyword(registry):
    """关键路径：name 作为字典键不再触发 TypeError。"""
    result = registry.call("query_product", arguments={"name": "极光笔记本"})

    assert result["found"] is True
    assert "极光笔记本" in result["name"]


def test_legacy_kwargs_form_is_rejected(registry):
    """旧调用形式必须报错，证明没有保留 **kwargs 兼容路径。"""
    with pytest.raises(TypeError):
        registry.call("query_product", name="极光笔记本")


def test_all_tools_callable_via_arguments(registry, monkeypatch):
    """8 个内置工具均通过 arguments 字典成功分发（写操作走两阶段确认）。"""
    _patch_write_source(monkeypatch)
    token = set_session_id("registry-args")
    try:
        assert registry.call("query_product", arguments={"name": "极光笔记本"})["found"] is True
        assert registry.call("query_order", arguments={"order_id": "20261101001"})["found"] is True
        assert (
            registry.call("query_logistics", arguments={"tracking_no": "SF2000000001"})["found"]
            is True
        )
        assert registry.call("search_faq", arguments={"question": "怎么退换货"})["found"] is True

        refund = registry.call(
            "apply_refund", arguments={"order_id": "20261103001", "reason": "不想要了"}
        )
        assert refund["needs_confirmation"] is True

        confirmed = registry.call(
            "confirm_action", arguments={"action_id": refund["action_id"]}
        )
        assert confirmed["success"] is True

        cancel = registry.call("cancel_order", arguments={"order_id": "20261103002"})
        assert cancel["needs_confirmation"] is True

        rejected = registry.call("reject_action", arguments={"action_id": cancel["action_id"]})
        assert rejected["success"] is True
    finally:
        reset_session_id(token)


def test_unknown_tool_raises_tool_execution_error(registry):
    """未注册工具仍按既有口径抛 ToolExecutionError。"""
    with pytest.raises(ToolExecutionError) as exc_info:
        registry.call("not_a_tool", arguments={})

    assert exc_info.value.code == "tool_failed"
    assert exc_info.value.details["tool"] == "not_a_tool"
    assert exc_info.value.details["args_keys"] == []


def test_disabled_tool_raises_tool_execution_error(registry):
    """禁用工具仍抛 ToolExecutionError，details.tool 不变。"""
    registry.disable("query_product")
    try:
        with pytest.raises(ToolExecutionError) as exc_info:
            registry.call("query_product", arguments={"name": "极光笔记本"})
        assert exc_info.value.details["tool"] == "query_product"
        assert exc_info.value.details["args_keys"] == ["name"]
    finally:
        # 恢复启用，保证独立注册表上的后续断言不受影响
        registry.enable("query_product")


def test_arguments_none_means_no_args(registry):
    """arguments=None 与 {} 等价，无参工具可以正常调用。"""
    registry.register(
        ToolSpec(
            name="no_args_tool",
            description="无参演示工具",
            parameters={"type": "object", "properties": {}},
            callable=lambda: {"ok": True},
        )
    )

    assert registry.call("no_args_tool") == {"ok": True}
    assert registry.call("no_args_tool", arguments=None) == {"ok": True}
    assert registry.call("no_args_tool", arguments={}) == {"ok": True}


def test_llm_client_call_tool_uses_arguments_dict():
    """_call_tool 的调用点已改为 arguments=...，可正确执行 query_product。"""
    # 绕过 __init__：本用例只验证工具分发，不需要真实 OpenAI 客户端
    bot = object.__new__(LLMClient)
    result = json.loads(bot._call_tool("query_product", '{"name": "极光笔记本"}'))

    assert result["found"] is True
    assert "极光笔记本" in result["name"]


def test_end_to_end_chat_product_query(monkeypatch):
    """/chat 的商品查询返回 200，不再出现 internal_error。"""
    monkeypatch.setattr(app_module, "bot", _RegistryProductBot())
    response = _post(
        "/chat",
        {"session_id": "registry-chat", "message": "极光笔记本多少钱"},
        "registry-chat-01",
    )

    assert response.status_code == 200
    body = response.json()
    assert "internal_error" not in json.dumps(body, ensure_ascii=False)
    assert "极光笔记本" in body["reply"]
    assert body["route"]["agent"] == "pre_sales"


def test_end_to_end_stream_product_query(monkeypatch):
    """/chat/stream 的商品查询无 error 事件，正常输出 delta。"""
    monkeypatch.setattr(app_module, "bot", _RegistryProductBot())
    response = _post(
        "/chat/stream",
        {"session_id": "registry-stream", "message": "极光笔记本多少钱"},
        "registry-stream-01",
    )

    assert response.status_code == 200
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert not [line for line in lines if line.get("event") == "error"]
    deltas = "".join(line["delta"] for line in lines if "delta" in line)
    assert "极光笔记本" in deltas

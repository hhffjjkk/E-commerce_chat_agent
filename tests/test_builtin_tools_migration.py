"""内置工具迁移保真测试：schema 哈希、注册表与 LLMClient 一致性、启停与参数异常。"""

import asyncio
import hashlib
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

from agent_system import tools  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.app import app  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.tool_registry import get_registry  # noqa: E402

# 8 条 schema 的完整快照哈希（阶段三步骤 4 追加 confirm_action / reject_action 后）
EXPECTED_SCHEMA_SHA256 = "c192bb5d8a06a9fc7e0e01640d35d1df631cd51bb9391f65b031d325788fb357"
# 原 6 条工具的快照哈希：用于证明追加两条后，原有 schema 逐字未变
LEGACY_SCHEMA_SHA256 = "4c46bb1ee6695d376e5821bb29ac28c5f8221964cbbaab7a6c1bfbb1eef9344e"

EXPECTED_NAMES = [
    "query_product",
    "query_order",
    "query_logistics",
    "search_faq",
    "apply_refund",
    "cancel_order",
    "confirm_action",
    "reject_action",
]


@pytest.fixture(autouse=True)
def temp_store(tmp_path):
    """用临时 SQLite 替换应用会话存储，避免写根目录 sessions.db。"""
    from agent_system import app as app_module
    from agent_system.memory import SessionStore

    original = app_module.store
    app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        app_module.store = original


def _request(method: str, path: str, *, json_body=None, headers=None):
    """通过 ASGITransport 调用应用，不启动真实服务器。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(run())


@app.post("/__test_tool_arguments")
def _bad_arguments():
    """测试路由：用非法 JSON 触发 Registry 参数解析失败。"""
    bot = object.__new__(LLMClient)
    return LLMClient._call_tool(bot, "query_order", "not-json")


def test_tool_schemas_are_migrated_verbatim():
    """原有 6 条 schema 逐字不变，新增 2 条后整体哈希与名称顺序正确。"""
    register_all()
    schemas = get_registry().schemas()
    payload = json.dumps(schemas, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_SCHEMA_SHA256
    assert [item["function"]["name"] for item in schemas] == EXPECTED_NAMES
    assert list(tools.TOOL_SCHEMAS) == EXPECTED_NAMES

    # 前 6 条必须与阶段三步骤 1 的快照完全一致
    legacy = json.dumps(schemas[:6], sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(legacy).hexdigest() == LEGACY_SCHEMA_SHA256


def test_llm_client_tools_matches_registry():
    """LLMClient 实例的 TOOLS 属性应与 Registry 输出一致。"""
    register_all()
    bot = object.__new__(LLMClient)
    assert bot.TOOLS == get_registry().schemas()
    assert len(bot.TOOLS) == 8


def test_call_tool_returns_json_string():
    """_call_tool 仍返回 JSON 字符串，业务结果与重构前一致。"""
    register_all()
    bot = object.__new__(LLMClient)
    raw = LLMClient._call_tool(bot, "query_order", json.dumps({"order_id": "20261101001"}))
    data = json.loads(raw)
    assert data["found"] is True
    assert data["order_id"] == "20261101001"


def test_disabled_tool_blocks_mock_chat():
    """禁用工具后，Mock 路径触发该工具应返回统一 500 tool_failed。"""
    registry = get_registry()
    registry.disable("query_order")
    try:
        response = _request(
            "POST",
            "/chat",
            json_body={"session_id": "disabled", "message": "查订单 20261101001"},
            headers={"X-Request-ID": "reg-disabled-0001"},
        )
    finally:
        registry.enable("query_order")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "tool_failed"
    assert body["details"]["tool"] == "query_order"


def test_invalid_arguments_do_not_leak_raw_json():
    """非法工具参数转成 tool_failed，且响应体不含原始字符串。"""
    response = _request(
        "POST",
        "/__test_tool_arguments",
        headers={"X-Request-ID": "reg-arguments-01"},
    )
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "tool_failed"
    assert body["details"]["tool"] == "query_order"
    assert "not-json" not in response.text

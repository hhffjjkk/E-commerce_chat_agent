# tests/test_errors.py
# 覆盖工具层 / LLM 层异常分类、统一错误响应、兜底钩子接入点与正常路径回归。

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

# 确保测试始终走 MockLLM + SQLite，避免误连真实 LLM/Redis
for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402

from agent_system import app as vendor_app_module  # noqa: E402
from agent_system import tools as tools_module  # noqa: E402
from agent_system.agent import LLMClient  # noqa: E402
from agent_system.errors import fallback  # noqa: E402
from agent_system.errors.exceptions import AgentError  # noqa: E402
from agent_system.errors.fallback import NoOpFallbackHook  # noqa: E402
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收进列表，便于断言 error_raised 等事件。"""

    def __init__(self):
        """初始化收集容器。"""
        super().__init__()
        self.records = []

    def emit(self, record):
        """收集一条日志记录。"""
        self.records.append(record)


@pytest.fixture
def captured_logs():
    """在应用 logger 上挂收集器，测试结束后恢复。"""
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
def temp_store(tmp_path):
    """用临时 SQLite 替换 vendor 会话存储，避免写真实数据库。"""
    from agent_system.memory import SessionStore

    original = vendor_app_module.store
    vendor_app_module.store = SessionStore(db_path=str(tmp_path / "sessions.db"))
    try:
        yield
    finally:
        vendor_app_module.store = original


def _events(handler: ListHandler) -> list[dict]:
    """把收集到的日志渲染成 JSON 后解析，得到结构化事件列表。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _raised(handler: ListHandler) -> list[dict]:
    """只挑出 error_raised 事件，便于断言错误码与是否可重试。"""
    return [event for event in _events(handler) if event["event"] == "error_raised"]


def _request(method: str, path: str, *, json_body=None, headers=None):
    """用 ASGITransport 直接调用应用；不吞异常就无法断言统一错误响应。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, headers=headers)

    return asyncio.run(run())


@app.post("/__test_errors_boom")
def _boom():
    """临时路由：制造未捕获异常，验证 internal_error 兜底。"""
    raise RuntimeError("boom for error test")


class _Completions:
    """模拟 openai 的 chat.completions，create 直接抛指定异常。"""

    def __init__(self, exc: Exception):
        """保存要抛出的异常实例。"""
        self.exc = exc

    def create(self, **kwargs):
        """无论参数如何都抛出预设异常，用于构造 LLM 失败场景。"""
        raise self.exc


class _FakeBot(LLMClient):
    """最小的 LLMClient 替身：复用真实 _call_llm 的埋点与异常映射。"""

    def __init__(self, exc: Exception):
        """用注入的异常构造假的 completions 链。"""
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(exc)))
        self.model = "fake-model"

    # 跳过真实工具循环，直接让 reply 走到最终 create 调用
    def _run_tool_loop(self, messages, *, profile=None):
        """原样返回 messages，避免触发工具循环的额外 create 调用。"""
        return messages

    def _build_messages(self, message, history, *, profile=None):
        """构造最小消息列表，避免依赖真实系统提示词。"""
        return [{"role": "user", "content": message}]

    def reply(self, message, history=None, *, profile=None):
        """非流式回复：复用真实 LLMClient 的 _call_llm（含埋点与异常映射）。"""
        return LLMClient.reply(self, message, history, profile=profile)

    def reply_stream(self, message, history=None, *, profile=None):
        """流式回复：内容相同，仅用于满足接口形状。"""
        yield self.reply(message, history, profile=profile)


class _StatusError(Exception):
    """带 status_code 的假异常，模拟 openai 的 APIStatusError 形态。"""

    def __init__(self, status_code: int):
        """记录状态码，供 _map_llm_error 判断是否可重试。"""
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _chat_error(captured_logs, request_id: str, message: str):
    """以指定 X-Request-ID 请求 /chat，便于把日志与响应头对齐断言。"""
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "err-session", "message": message},
        headers={"X-Request-ID": request_id},
    )
    return response


def test_tool_value_error_maps_to_tool_failed(captured_logs, monkeypatch):
    """工具抛普通异常：500 + tool_failed + 可重试，details 带上出错的工具名。"""

    def boom(order_id):
        """模拟工具内部实现出错。"""
        raise ValueError("bad order payload")

    # 直接替换数据源方法，触发真实的工具包装链路
    monkeypatch.setattr(tools_module._source, "query_order", boom)
    request_id = "err-tool-0000001"
    response = _chat_error(captured_logs, request_id, "查订单 20261006001")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "tool_failed"
    assert body["retryable"] is True
    assert body["details"]["tool"] == "query_order"
    assert body["request_id"] == request_id
    assert response.headers["x-request-id"] == request_id

    raised = _raised(captured_logs)
    assert len(raised) == 1
    assert raised[0]["request_id"] == request_id
    assert raised[0]["code"] == "tool_failed"
    # 只记录原始异常类型，不记录异常文本
    assert raised[0]["cause_type"] == "ValueError"


def test_tool_file_not_found_maps_to_tool_failed(captured_logs, monkeypatch):
    """数据文件缺失：归为 tool_failed，details 带工具名与参数键。"""

    def boom(order_id):
        """模拟数据文件不存在。"""
        raise FileNotFoundError("orders.json missing")

    monkeypatch.setattr(tools_module._source, "query_order", boom)
    request_id = "err-file-0000001"
    response = _chat_error(captured_logs, request_id, "查订单 20261006002")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "tool_failed"
    assert body["details"]["tool"] == "query_order"
    assert body["details"]["args_keys"] == ["order_id"]

    raised = _raised(captured_logs)
    assert raised and raised[0]["cause_type"] == "FileNotFoundError"


def test_llm_timeout_maps_to_llm_failed_retryable(captured_logs, monkeypatch):
    """LLM 超时：502 + llm_failed + 可重试（超时属于暂时性故障）。"""
    bot = _FakeBot(TimeoutError("timed out"))
    # 替换应用级 bot，使 /chat 走这个假客户端
    monkeypatch.setattr(vendor_app_module, "bot", bot)

    request_id = "err-llm-timeout-01"
    response = _chat_error(captured_logs, request_id, "你好")

    assert response.status_code == 502
    body = response.json()["error"]
    assert body["code"] == "llm_failed"
    assert body["retryable"] is True
    assert body["request_id"] == request_id
    assert response.headers["x-request-id"] == request_id

    raised = _raised(captured_logs)
    assert raised and raised[0]["code"] == "llm_failed"
    assert raised[0]["cause_type"] == "TimeoutError"


def test_llm_400_maps_to_non_retryable(captured_logs, monkeypatch):
    """LLM 返回 4xx（非 429）：错误码仍是 llm_failed，但标记为不可重试。"""
    bot = _FakeBot(_StatusError(400))
    monkeypatch.setattr(vendor_app_module, "bot", bot)

    request_id = "err-llm-400-0001"
    response = _chat_error(captured_logs, request_id, "你好")

    assert response.status_code == 502
    body = response.json()["error"]
    assert body["code"] == "llm_failed"
    assert body["retryable"] is False
    assert body["request_id"] == request_id


def test_missing_message_returns_validation_error(captured_logs):
    """缺字段：400 + validation_error + 不可重试，details 指出缺失的字段名。"""
    request_id = "err-valid-000001"
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "v1"},
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["retryable"] is False
    assert body["details"]["fields"] == ["message"]
    assert body["request_id"] == request_id
    assert response.headers["x-request-id"] == request_id

    raised = _raised(captured_logs)
    assert raised and raised[0]["request_id"] == request_id
    assert raised[0]["code"] == "validation_error"


def test_unhandled_exception_returns_internal_error(captured_logs):
    """未分类异常：internal_error，且响应体绝不包含原始异常文本与 cause 字段。"""
    request_id = "err-internal-0001"
    response = _request(
        "POST",
        "/__test_errors_boom",
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 500
    raw = response.text
    # 两条防线：不泄露异常消息，不出现 cause 字段（cause 只进日志）
    assert "boom for error test" not in raw
    assert "cause" not in raw
    assert response.json() == {"error": {"code": "internal_error", "request_id": request_id}}
    assert response.headers["x-request-id"] == request_id

    raised = _raised(captured_logs)
    assert raised and raised[0]["request_id"] == request_id
    assert raised[0]["code"] == "internal_error"
    assert raised[0]["cause_type"] == "RuntimeError"


def test_fallback_hook_is_invoked_once(captured_logs, monkeypatch):
    """兜底钩子接入点：注册后必须被调用且只调用一次，context 携带定位信息。"""

    class RecordingHook:
        """记录每次调用的测试钩子，行为等价于 NoOp。"""

        def __init__(self):
            """初始化调用记录。"""
            self.calls = []

        async def handle(self, error, context):
            """记录 (error, context) 后原样返回错误。"""
            self.calls.append((error, context))
            return error

    def boom(order_id):
        """模拟工具异常，触发钩子调用。"""
        raise ValueError("hook test")

    monkeypatch.setattr(tools_module._source, "query_order", boom)
    hook = RecordingHook()
    fallback.register_hook(hook)
    try:
        request_id = "err-hook-0000001"
        response = _chat_error(captured_logs, request_id, "查订单 20261006003")
        assert response.status_code == 500
        # 恰好一次：多于一次说明重复注册或重复包装
        assert len(hook.calls) == 1
        error, context = hook.calls[0]
        assert isinstance(error, AgentError)
        assert context["request_id"] == request_id
        assert context["path"] == "/chat"
        assert context["tool"] == "query_order"
    finally:
        # 恢复默认钩子，避免影响其他用例
        fallback.register_hook(NoOpFallbackHook())


def test_mock_mode_normal_path_still_works(captured_logs):
    """Mock 模式（无 client）下正常请求不应报错，也不应产生任何 error_raised。"""
    response = _request(
        "POST",
        "/chat",
        json_body={"session_id": "ok-session", "message": "你好"},
    )
    assert response.status_code == 200
    assert "reply" in response.json()
    assert _raised(captured_logs) == []

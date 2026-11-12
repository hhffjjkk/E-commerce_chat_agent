# tests/test_rate_limit.py
# 限流与请求体大小限制：阈值、Retry-After、豁免路径、OPTIONS 不计数与总开关。

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 清空外部依赖配置，保证测试完全本地、可重复
for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402
from agent_system.obs.request_id import RequestContextMiddleware  # noqa: E402
from agent_system.runtime.body_limit import BodyLimitMiddleware  # noqa: E402
from agent_system.runtime.health import register_health_routes  # noqa: E402
from agent_system.runtime.rate_limit import RateLimitMiddleware  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收进列表，便于断言 rate_limited 事件。"""

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


def _events(handler: ListHandler) -> list[dict]:
    """把收集到的日志渲染成 JSON 后解析。"""
    return [json.loads(handler.format(record)) for record in handler.records]


def _request(app, method: str, path: str, *, headers=None, json_body=None, content=None):
    """用 ASGITransport 请求指定应用实例（不占端口、无需启动服务器）。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(
                method, path, headers=headers, json=json_body, content=content
            )

    return asyncio.run(run())


def _guarded_app(**rate_limit_kwargs):
    """构造一个最小应用：只挂限流 + 请求上下文中间件，外加健康检查路由。

    注意注册顺序与 main.py 一致：先限流后 request_id，
    这样 request_id 才在最外层，429 响应也带 X-Request-ID。
    """
    app = FastAPI()
    register_health_routes(app)

    @app.post("/chat")
    def chat():
        """最小业务端点，用于验证请求是否被放行。"""
        return {"reply": "ok"}

    app.add_middleware(RateLimitMiddleware, **rate_limit_kwargs)
    app.add_middleware(RequestContextMiddleware)
    return app


def _post(app, request_id: str):
    """带指定 X-Request-ID 请求 /chat，便于把响应头与日志对齐。"""
    return _request(
        app,
        "POST",
        "/chat",
        json_body={"session_id": "s1", "message": "hi"},
        headers={"X-Request-ID": request_id},
    )


def test_third_request_is_rate_limited(captured_logs):
    """阈值 2：前两次放行，第三次返回 429，且带 Retry-After 与 request_id。"""
    app = _guarded_app(
        enabled=True,
        window_seconds=60,
        max_requests=2,
        exclude_paths=("/health", "/health/ready"),
        cors_allow_origins=(),
    )

    first = _post(app, "rl-000000000001")
    second = _post(app, "rl-000000000002")
    third = _post(app, "rl-000000000003")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429

    body = third.json()["error"]
    assert body["code"] == "rate_limited"
    assert body["retryable"] is True
    assert body["request_id"] == "rl-000000000003"
    # 关键：最外层 request_id 中间件仍能给限流响应加头
    assert third.headers["x-request-id"] == "rl-000000000003"
    assert int(third.headers["retry-after"]) >= 1

    events = [e for e in _events(captured_logs) if e["event"] == "rate_limited"]
    assert len(events) == 1
    assert events[0]["request_id"] == "rl-000000000003"
    assert events[0]["path"] == "/chat"
    assert events[0]["limit"] == 2
    assert events[0]["window"] == 60


def test_health_paths_are_excluded(captured_logs):
    """健康检查路径不计入配额，避免探针把正常流量限死。"""
    app = _guarded_app(
        enabled=True,
        window_seconds=60,
        max_requests=1,
        exclude_paths=("/health", "/health/ready"),
        cors_allow_origins=(),
    )

    for _ in range(3):
        assert _request(app, "GET", "/health").status_code == 200
        assert _request(app, "GET", "/health/ready").status_code == 200

    # 探针请求没有消耗配额，所以第一次业务请求仍然放行
    assert _post(app, "rl-exclude-00001").status_code == 200
    assert _post(app, "rl-exclude-00002").status_code == 429


def test_options_preflight_is_not_counted(captured_logs):
    """CORS 预检（OPTIONS）不计数，否则浏览器会把真实请求的配额吃掉。"""
    app = _guarded_app(
        enabled=True,
        window_seconds=60,
        max_requests=1,
        exclude_paths=(),
        cors_allow_origins=(),
    )

    for _ in range(3):
        _request(app, "OPTIONS", "/chat")

    assert _post(app, "rl-options-00001").status_code == 200
    assert _post(app, "rl-options-00002").status_code == 429


def test_rate_limit_disabled(captured_logs):
    """总开关关闭时，任何次数都不应被拒。"""
    app = _guarded_app(
        enabled=False,
        window_seconds=60,
        max_requests=1,
        exclude_paths=(),
        cors_allow_origins=(),
    )

    assert _post(app, "rl-disabled-00001").status_code == 200
    assert _post(app, "rl-disabled-00002").status_code == 200


def test_body_limit_rejects_large_content_length(captured_logs):
    """请求体超过上限：400 validation_error，details 带上限值，且响应带 request_id。"""
    app = FastAPI()

    @app.post("/chat")
    def chat():
        """最小业务端点；理论上不应被调用到。"""
        return {"reply": "ok"}

    app.add_middleware(BodyLimitMiddleware, max_bytes=1024)
    app.add_middleware(RequestContextMiddleware)

    # content 会生成 Content-Length=2048，超过 1024 的上限
    response = _request(
        app,
        "POST",
        "/chat",
        content=b"x" * 2048,
        headers={"Content-Type": "application/json", "X-Request-ID": "body-limit-00001"},
    )

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["message"] == "请求体过大"
    assert body["details"]["max_bytes"] == 1024
    assert body["request_id"] == "body-limit-00001"
    assert response.headers["x-request-id"] == "body-limit-00001"

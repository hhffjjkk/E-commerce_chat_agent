# tests/test_cors.py
# CORS：默认不注册；配置白名单后仅对匹配 Origin 返回 CORS 头且不带凭证。

import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.config import get_settings  # noqa: E402
from agent_system.app import app as global_app  # noqa: E402
from agent_system.runtime.cors import configure_cors  # noqa: E402


def _request(app, method: str, path: str, *, headers=None, json_body=None):
    """用 ASGITransport 请求指定应用实例。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, json=json_body)

    return asyncio.run(run())


def _cors_app(origins):
    """构造一个只配置了 CORS 白名单的最小应用，避免受真实配置影响。"""
    app = FastAPI()

    @app.post("/chat")
    def chat():
        """最小业务端点，用于观察响应头。"""
        return {"reply": "ok"}

    # replace 生成新的 Settings 副本，只改白名单，其余沿用当前配置
    settings = replace(get_settings(), cors_allow_origins=tuple(origins))
    assert configure_cors(app, settings) is True
    return app


def test_cors_not_registered_by_default():
    """默认（未配置 CORS_ALLOW_ORIGINS）不应返回任何 CORS 头。"""
    response = _request(
        global_app,
        "OPTIONS",
        "/chat",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_configure_cors_noop_when_origins_empty():
    """白名单为空时应直接跳过注册，不往中间件栈里加任何东西。"""
    app = FastAPI()
    settings = replace(get_settings(), cors_allow_origins=())
    assert configure_cors(app, settings) is False
    assert app.user_middleware == []


def test_cors_allowed_origin_on_post():
    """白名单内的 Origin：返回对应 CORS 头，且不出现 allow-credentials。"""
    app = _cors_app(("http://example.com",))
    response = _request(
        app,
        "POST",
        "/chat",
        headers={"Origin": "http://example.com"},
        json_body={},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://example.com"
    assert "access-control-allow-credentials" not in response.headers


def test_cors_rejects_unknown_origin():
    """白名单外的 Origin：业务照常返回，但不带 CORS 头，浏览器侧会被拦。"""
    app = _cors_app(("http://example.com",))
    response = _request(
        app,
        "POST",
        "/chat",
        headers={"Origin": "http://evil.example"},
        json_body={},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers

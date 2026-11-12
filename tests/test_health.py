# tests/test_health.py
# 健康检查：存活/就绪状态、依赖检查结果与 health_check 日志事件。

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 不配置 Redis，验证「未配置视为不检查」的分支
for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "REDIS_URL"):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402  （导入时完成 vendor 路径注入）
from agent_system.obs.logging_config import JSONFormatter, get_logger  # noqa: E402
from agent_system.runtime import health  # noqa: E402


class ListHandler(logging.Handler):
    """把日志记录收进列表，便于断言 health_check 事件。"""

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


def _get(path: str, request_id: str):
    """带指定 X-Request-ID 发起 GET，便于断言 request_id 贯穿。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def test_liveness_ok(captured_logs):
    """存活检查：恒 200，返回服务名与 request_id，并记录 liveness 事件。"""
    request_id = "health-live-0001"
    response = _get("/health", request_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"]
    assert body["request_id"] == request_id
    assert response.headers["x-request-id"] == request_id

    events = [e for e in _events(captured_logs) if e["event"] == "health_check"]
    assert events and events[-1]["kind"] == "liveness"
    assert events[-1]["status"] == "ok"
    assert events[-1]["request_id"] == request_id


def test_readiness_ok_without_redis(captured_logs):
    """未配置 Redis 时视为不检查：整体仍为 ok，data 目录需可读。"""
    request_id = "health-ready-001"
    response = _get("/health/ready", request_id)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["redis"] == {"ok": True, "detail": "not_configured"}
    assert body["checks"]["data"]["ok"] is True
    assert "readable" in body["checks"]["data"]["detail"]
    assert body["request_id"] == request_id
    assert response.headers["x-request-id"] == request_id

    events = [e for e in _events(captured_logs) if e["event"] == "health_check"]
    assert events and events[-1]["kind"] == "readiness"
    assert events[-1]["status"] == "ok"
    assert events[-1]["checks"]["data"]["ok"] is True


def test_readiness_down_when_data_unreadable(captured_logs, monkeypatch):
    """数据目录不可读属于关键故障：就绪返回 503 且 status=down。"""
    # 直接替换模块级函数，避免真的去破坏文件系统
    monkeypatch.setattr(
        health,
        "check_data_dir",
        lambda: {"ok": False, "detail": "data dir missing"},
    )
    request_id = "health-ready-002"
    response = _get("/health/ready", request_id)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    assert body["checks"]["data"]["ok"] is False
    assert body["request_id"] == request_id

    events = [e for e in _events(captured_logs) if e["event"] == "health_check"]
    assert events and events[-1]["status"] == "down"

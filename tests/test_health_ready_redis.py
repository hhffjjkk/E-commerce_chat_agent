"""健康检查的 Redis 分支：未配置、可达、不可达，以及 force=True 语义。"""

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest
import redis as redis_module

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("REDIS_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.app import app  # noqa: E402
from agent_system.cache import reset_redis_manager  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.runtime import health  # noqa: E402


class _FakeRedis:
    """只实现 ping/close 的 Redis 替身。"""

    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.pings = 0
        self.closed = False

    def ping(self):
        self.pings += 1
        if self.error is not None:
            raise self.error
        return True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolate_manager(monkeypatch):
    """重置单例与配置缓存，避免用例之间共享连接与 PING 缓存。"""
    reset_redis_manager()
    get_settings.cache_clear()
    yield
    reset_redis_manager()
    get_settings.cache_clear()


def _get(path: str, request_id: str):
    """通过 ASGITransport 发起 GET。"""

    async def run():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers={"X-Request-ID": request_id})

    return asyncio.run(run())


def test_check_redis_not_configured(monkeypatch):
    """未配置 REDIS_URL：视为不检查，detail 保持 not_configured。"""
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert health.check_redis(get_settings()) == {
        "ok": True,
        "detail": "not_configured",
    }


def test_check_redis_reachable_always_pings(monkeypatch):
    """可达：ok=true；每次检查都 force PING，不受 5 秒缓存影响。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)
    settings = get_settings()

    first = health.check_redis(settings)
    second = health.check_redis(settings)

    assert first == {"ok": True, "detail": "pong"}
    assert second == {"ok": True, "detail": "pong"}
    assert fake.pings == 2


def test_check_redis_unreachable_timeout(monkeypatch):
    """不可达：ok=false，detail 为归类后的原因码。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    fake = _FakeRedis(error=TimeoutError("timeout"))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    assert health.check_redis(get_settings()) == {"ok": False, "detail": "timeout"}


def test_readiness_degraded_but_liveness_ok(monkeypatch):
    """Redis 不可达：/health/ready 503 degraded，/health 仍 200。"""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1")
    fake = _FakeRedis(error=ConnectionError("boom"))
    monkeypatch.setattr(redis_module, "from_url", lambda *args, **kwargs: fake)

    ready = _get("/health/ready", "health-redis-01")
    assert ready.status_code == 503
    body = ready.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"] == {"ok": False, "detail": "connect_error"}
    assert body["checks"]["data"]["ok"] is True

    live = _get("/health", "health-redis-02")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

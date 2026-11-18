"""压测执行器测试：低压强端到端、错误注入、中止保护、隔离与清理。"""

import asyncio
import hashlib
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 app / load_runner 之前清掉外部密钥，保证默认走 Mock
for _key in (
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
):
    os.environ.pop(_key, None)

from agent_system import app as app_module  # noqa: E402
from agent_system.eval import load_runner  # noqa: E402
from agent_system.eval.collector import EvalCollector  # noqa: E402
from agent_system.eval.load_metrics import RequestRecord  # noqa: E402
from agent_system.eval.load_scenarios import (  # noqa: E402
    SCENARIO_CACHE_OFF,
    SCENARIO_CACHE_ON_REPEAT_05,
)
from agent_system.eval.runner import _EvalEnvironment  # noqa: E402


def _orders_hash() -> str:
    """计算 data/orders.json 的 SHA256，用于确认压测未修改数据。"""
    return hashlib.sha256(
        (ROOT / "data" / "orders.json").read_bytes()
    ).hexdigest()


def test_cache_off_low_pressure_end_to_end():
    """并发 2、20 请求的基线场景：无错误、无缓存命中、数据文件不变。"""
    scenario = replace(
        SCENARIO_CACHE_OFF,
        concurrency_levels=(2,),
        requests_per_level=20,
    )
    before = _orders_hash()

    results = load_runner.run_scenarios([scenario], mode="mock", seed=42)

    assert len(results) == 1
    level = results[0].levels[0]
    assert level.total_requests == 20
    assert level.success == 20
    assert level.error_rate == 0.0
    assert level.cache_hit_rate == 0.0
    assert results[0].aborted is None
    assert _orders_hash() == before


def test_cache_on_low_pressure_has_hits():
    """缓存场景：重复消息应在第二次请求命中缓存。"""
    scenario = replace(
        SCENARIO_CACHE_ON_REPEAT_05,
        concurrency_levels=(2,),
        requests_per_level=20,
    )

    results = load_runner.run_scenarios([scenario], mode="mock", seed=42)

    level = results[0].levels[0]
    assert level.total_requests == 20
    assert level.cache_hit_rate > 0
    assert results[0].scenario.cache_enabled is True


def test_error_rate_below_threshold_does_not_abort(monkeypatch):
    """20% 请求失败：错误率被统计，但不应触发中止。"""

    async def fake_request(client, path, session_id, message):
        index = int(session_id.rsplit("-", 1)[1])
        if index >= 8:
            raise RuntimeError("injected")
        now = time.monotonic()
        return RequestRecord(now, now + 0.001, 200, 1.0)

    monkeypatch.setattr(load_runner, "_perform_request", fake_request)
    scenario = replace(
        SCENARIO_CACHE_OFF, concurrency_levels=(1,), requests_per_level=10
    )

    results = load_runner.run_scenarios([scenario], mode="mock", seed=42)

    level = results[0].levels[0]
    assert level.error_rate == 0.2
    assert results[0].aborted is None


def test_error_rate_above_threshold_aborts_remaining_levels(monkeypatch):
    """60% 请求失败：记录 error_rate_exceeded，并停止后续并发度。"""

    async def fake_request(client, path, session_id, message):
        index = int(session_id.rsplit("-", 1)[1])
        if index >= 4:
            raise RuntimeError("injected")
        now = time.monotonic()
        return RequestRecord(now, now + 0.001, 200, 1.0)

    monkeypatch.setattr(load_runner, "_perform_request", fake_request)
    scenario = replace(
        SCENARIO_CACHE_OFF, concurrency_levels=(1, 2), requests_per_level=10
    )

    results = load_runner.run_scenarios([scenario], mode="mock", seed=42)

    assert results[0].aborted == "error_rate_exceeded"
    assert len(results[0].levels) == 1
    assert results[0].levels[0].error_rate == 0.6


def test_each_request_uses_unique_session_id(monkeypatch):
    """每个请求的 session_id 必须唯一，避免历史累积影响延迟。"""
    original = load_runner._perform_request
    seen: set[str] = set()

    async def spy(client, path, session_id, message):
        assert session_id not in seen
        seen.add(session_id)
        return await original(client, path, session_id, message)

    monkeypatch.setattr(load_runner, "_perform_request", spy)
    scenario = replace(
        SCENARIO_CACHE_OFF, concurrency_levels=(2,), requests_per_level=10
    )

    load_runner.run_scenarios([scenario], mode="mock", seed=42)

    # 预热请求使用 load-warmup-* 前缀且不计入指标；被测请求必须各自独立
    measured = {session_id for session_id in seen if not session_id.startswith("load-warmup-")}
    assert len(measured) == 10


def test_scenario_cache_keys_are_cleared_between_scenarios():
    """缓存场景会写入 nmsg key；下一场景开始前按前缀清理。"""
    with _EvalEnvironment("mock") as environment:
        app_module.store = load_runner._ConcurrentSessionStore()
        base_settings = app_module.settings

        load_runner._prepare_scenario(
            environment, base_settings, SCENARIO_CACHE_ON_REPEAT_05
        )
        cache_scenario = replace(
            SCENARIO_CACHE_ON_REPEAT_05,
            concurrency_levels=(1,),
            requests_per_level=6,
        )
        asyncio.run(
            load_runner.run_scenario(
                cache_scenario,
                environment=environment,
                collector=EvalCollector(),
            )
        )
        nmsg_keys = [
            key for key in environment.fake_redis.data if str(key).startswith("nmsg:")
        ]
        assert nmsg_keys

        load_runner._prepare_scenario(
            environment, base_settings, SCENARIO_CACHE_OFF
        )
        assert not [
            key
            for key in environment.fake_redis.data
            if str(key).startswith(("nmsg:", "session_state:"))
        ]

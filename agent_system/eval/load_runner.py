"""并发压测执行器：asyncio + httpx 直接压 ASGI app。

为什么用 asyncio + httpx 而不是 locust/k6:
    `httpx.ASGITransport` 可以直接把请求送进 ASGI app，不需要启动端口与第二个进程，
    避免端口管理、跨进程统计与额外依赖；工具链与步骤 1 的评测器一致。

为什么默认 in-process:
    压测目标是“业务编排与缓存的并发表现”，不是真实网络栈。in-process 去掉了
    TCP/序列化噪声，便于观察缓存收益；真实网络压测列入遗留。

请求级指标为什么从响应提取:
    并发下 `log_event` 的 request_id 关联需要在 collector 里做额外过滤，
    而 `/chat` 响应体与 `/chat/stream` 的 NDJSON 已包含 cache/route 字段；
    collector 仅服务步骤 1 的正确性评测。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from agent_system import app as app_module
from agent_system.app import app
from agent_system.cache import (
    get_nmsg_cache,
    get_session_state_cache,
    reset_nmsg_cache,
    reset_session_state_cache,
)
from agent_system.config import PROJECT_ROOT
from agent_system.eval.collector import EvalCollector
from agent_system.eval.load_metrics import (
    LoadScenarioResult,
    LevelStats,
    RequestRecord,
    aggregate_level,
)
from agent_system.eval.load_reporter import write_json_report, write_markdown_report
from agent_system.eval.load_scenarios import (
    DEFAULT_SCENARIO_NAMES,
    MOCK_LLM_ORCHESTRATION_MS,
    LoadMessage,
    LoadScenario,
    build_request_sequence,
    load_messages,
    resolve_requests,
    select_scenarios,
)
from agent_system.eval.runner import _EvalEnvironment
from agent_system.harness.guardrails import reset_guardrails
from agent_system.harness.pending_store import reset_pending_store

# 单个请求的超时秒数；超时记为错误，不中断整轮压测
REQUEST_TIMEOUT_SECONDS = 30.0

# 错误率超过该阈值时中止当前场景，避免把故障当成性能数据
ABORT_ERROR_RATE = 0.5

_DEFAULT_MESSAGES = PROJECT_ROOT / "evals" / "load_messages.jsonl"


class _ConcurrentSessionStore:
    """压测用线程安全内存会话库。

    为什么不用临时 SQLite:
        `/chat` 是同步端点，FastAPI 会在线程池中并发执行；SQLite 共享连接在
        高并发写入时会产生锁竞争/`MAX(seq)` 竞争，污染延迟与错误率。
        压测只关心单请求链路，历史消息用内存字典即可，且不需要持久化。
    """

    def __init__(self, max_history: int = 20):
        """初始化空存储。"""
        self.max_history = max_history
        self._data: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> list[dict]:
        """返回该会话最近的历史消息（按时间正序）。"""
        with self._lock:
            history = list(self._data.get(session_id, []))
        return history[-self.max_history :]

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加消息并裁剪到 max_history。"""
        with self._lock:
            history = self._data.setdefault(session_id, [])
            history.append({"role": role, "content": content})
            if len(history) > self.max_history:
                del history[: -self.max_history]

    def clear(self, session_id: str) -> None:
        """清空某个会话。"""
        with self._lock:
            self._data.pop(session_id, None)


class _LatencyBot:
    """压测用 bot 包装：为 cache-miss 的真实回复注入固定的模拟编排耗时。

    说明:
        只在 Mock 压测中启用；通过 `__getattr__` 透传 `_last_used_tools`
        与 `schemas_for`，保证 app 层的缓存写回判断不受影响。
    """

    def __init__(self, inner, delay_ms: float):
        """保存真实 bot 与模拟耗时（毫秒）。"""
        self._inner = inner
        self._delay_seconds = max(0.0, float(delay_ms)) / 1000.0

    def reply(self, message, history=None, *, profile=None):
        """等待模拟耗时后返回真实 Mock 回复。"""
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        return self._inner.reply(message, history=history, profile=profile)

    def reply_stream(self, message, history=None, *, profile=None):
        """流式路径同样注入耗时（影响 TTFB）。"""
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        yield from self._inner.reply_stream(message, history=history, profile=profile)

    def __getattr__(self, name):
        """其余属性（如 _last_used_tools / schemas_for）透传给真实 bot。"""
        return getattr(self._inner, name)


def _has_api_key() -> bool:
    """是否配置了真实 LLM 的 API Key。"""
    return bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))


def _disable_rate_limit() -> list[tuple[object, bool]]:
    """临时关闭进程内限流中间件，返回原始开关以便恢复。

    返回:
        [(middleware, 原始 enabled)]。

    说明:
        限流是部署策略（默认 120 次/分钟），压测会很快触发 429，
        从而把“限流保护”误读成“业务错误”。压测目标是业务编排与缓存，
        因此在本轮压测期间关闭限流，并清空已有计数桶；结束后恢复。
        该操作只改内存中的中间件实例，不修改业务代码或配置。
    """
    from agent_system.runtime.rate_limit import RateLimitMiddleware

    restored: list[tuple[object, bool]] = []
    # Starlette 的 middleware_stack 默认惰性构建（首个请求前为 None）；
    # 压测会触发构建，这里提前构建一次，保证能拿到限流中间件实例。
    if getattr(app, "middleware_stack", None) is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, RateLimitMiddleware):
            restored.append((node, node.enabled))
            node.enabled = False
            node._buckets.clear()
        node = getattr(node, "app", None)
    return restored


def _restore_rate_limit(restored: list[tuple[object, bool]]) -> None:
    """恢复限流中间件的原始开关。"""
    for middleware, enabled in restored:
        middleware.enabled = enabled
        middleware._buckets.clear()


def _silence_info_logs():
    """压测期间把应用日志级别临时提升到 WARNING。

    返回:
        原日志级别，供恢复使用。

    说明:
        in-process 压测没有网络耗时，INFO 级结构化日志的序列化与终端输出
        会成为主要成本，掩盖业务与缓存差异。压测只关心链路性能，因此临时
        关闭 INFO 日志；生产环境的 QPS 会因日志 I/O 低于本次结果。
    """
    import logging

    from agent_system.obs.logging_config import get_logger

    logger = get_logger()
    original = logger.level
    logger.setLevel(logging.WARNING)
    return logger, original


def _restore_log_level(state) -> None:
    """恢复压测前的日志级别。"""
    logger, original = state
    logger.setLevel(original)


def _clear_eval_cache(environment: _EvalEnvironment) -> None:
    """按前缀精确清理本次压测写入的缓存 key。

    参数:
        environment: 步骤 1 的隔离环境。

    说明:
        只删除 FakeRedis 字典中 `nmsg:` / `session_state:` 前缀的 key，
        不使用 FLUSHDB（FakeRedis 也没有该命令），不会触碰任何真实实例数据。
    """
    fake = getattr(environment, "fake_redis", None)
    if fake is None:
        return
    for key in list(fake.data):
        text = str(key)
        if text.startswith("nmsg:") or text.startswith("session_state:"):
            fake.data.pop(key, None)


def _prepare_scenario(
    environment: _EvalEnvironment,
    base_settings,
    scenario: LoadScenario,
) -> None:
    """场景开始前重置缓存单例并按场景切换缓存开关。

    参数:
        environment: 隔离环境（提供 FakeRedis）。
        base_settings: 进入环境后的 settings 快照（保留其它配置）。
        scenario: 当前场景。

    说明:
        `app.py` 在每次请求时读取模块级 `settings.nmsg_cache_enabled`，
        因此替换 app 模块的 settings 即可在运行期切换缓存，无需改业务代码。
    """
    _clear_eval_cache(environment)
    reset_nmsg_cache()
    reset_session_state_cache()
    reset_guardrails()
    reset_pending_store()

    app_module.nmsg_cache = get_nmsg_cache()
    app_module.state_cache = get_session_state_cache()
    app_module.settings = replace(
        base_settings, nmsg_cache_enabled=scenario.cache_enabled
    )


def _environment_snapshot(environment: _EvalEnvironment) -> dict:
    """记录场景结束时的缓存后端状态，便于复现。"""
    fake = getattr(environment, "fake_redis", None)
    if fake is None:
        return {"backend": "real_or_disabled", "fake_redis": False, "key_count": None}
    return {
        "backend": "fake_redis",
        "fake_redis": True,
        "key_count": len(fake.data),
    }


def _error_record(started: float, error: str) -> RequestRecord:
    """构造一条失败请求记录。"""
    finished = time.perf_counter()
    return RequestRecord(
        started_at=started,
        finished_at=finished,
        status_code=0,
        latency_ms=(finished - started) * 1000.0,
        error=error,
    )


async def _perform_request(
    client: httpx.AsyncClient,
    path: str,
    session_id: str,
    message: str,
) -> RequestRecord:
    """执行一次 HTTP 请求并读取缓存/路由信息。

    参数:
        client: 共享的异步客户端。
        path: "/chat" 或 "/chat/stream"。
        session_id: 本次请求的独立会话 ID。
        message: 用户消息。

    返回:
        RequestRecord。

    说明:
        - `/chat`：从响应 JSON 的 cache.hit / route.agent 读取；
        - `/chat/stream`：完整消费 NDJSON，记录首字节延迟（TTFB）与整体延迟；
        - `llm_calls` 在 Mock 模式下恒为 0；响应协议不暴露真实调用次数，
          因此这里保持 0，报告中会标注该局限。
    """
    body = {"session_id": session_id, "message": message}
    # Windows 下 time.monotonic() 分辨率约 15.6ms，会把延迟量化成 0/16/31ms；
    # 性能测量改用 perf_counter（高精度、同进程内单调），否则 P50/P95 不可读。
    started = time.perf_counter()

    if path == "/chat":
        response = await client.post(path, json=body)
        finished = time.perf_counter()
        cache_hit = False
        route_agent = None
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            cache = payload.get("cache") or {}
            route = payload.get("route") or {}
            cache_hit = bool(cache.get("hit"))
            route_agent = route.get("agent")
        error = None if 200 <= response.status_code < 300 else f"http_{response.status_code}"
        return RequestRecord(
            started_at=started,
            finished_at=finished,
            status_code=response.status_code,
            latency_ms=(finished - started) * 1000.0,
            cache_hit=cache_hit,
            route_agent=route_agent,
            error=error,
        )

    # 流式路径：TTFB 取第一行非空 NDJSON 的时间，整体延迟取流消费结束时间
    ttfb_ms = None
    cache_hit = False
    route_agent = None
    async with client.stream("POST", path, json=body) as response:
        status_code = response.status_code
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            if ttfb_ms is None:
                ttfb_ms = (time.perf_counter() - started) * 1000.0
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            if event == "cache":
                cache_hit = bool(payload.get("hit"))
            elif event == "route":
                route_agent = payload.get("agent")
            elif payload.get("done") is True or event == "done":
                break

    finished = time.perf_counter()
    error = None if 200 <= status_code < 300 else f"http_{status_code}"
    return RequestRecord(
        started_at=started,
        finished_at=finished,
        status_code=status_code,
        latency_ms=(finished - started) * 1000.0,
        ttfb_ms=ttfb_ms,
        cache_hit=cache_hit,
        route_agent=route_agent,
        error=error,
    )


async def _request_worker(
    client: httpx.AsyncClient,
    path: str,
    session_id: str,
    message: str,
) -> RequestRecord:
    """带超时与异常兜底的单请求执行。

    参数:
        client: 共享客户端。
        path: 目标路径。
        session_id: 独立会话 ID。
        message: 用户消息。

    返回:
        RequestRecord；异常被转换为 error 字段，不向上抛出。

    说明:
        单个请求失败不能中断整轮压测；超时统一标记为 "timeout"。
    """
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _perform_request(client, path, session_id, message),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _error_record(started, "timeout")
    except Exception as exc:  # noqa: BLE001 - 压测必须继续
        return _error_record(started, type(exc).__name__)


async def run_scenario(
    scenario: LoadScenario,
    *,
    environment: _EvalEnvironment,
    collector: EvalCollector,
    progress=None,
    messages: list[LoadMessage] | None = None,
    seed: int | None = None,
    requests_per_worker: int = 25,
) -> LoadScenarioResult:
    """执行一个场景的全部并发度。

    参数:
        scenario: 压测场景。
        environment: 步骤 1 的隔离环境（提供 FakeRedis 与 settings 覆盖）。
        collector: 事件采集器；并发下不做请求级归集，保留参数用于接口一致。
        progress: 可选进度回调，接收一行中文文本。
        messages: 消息池；None 时读取 evals/load_messages.jsonl。
        seed: 覆盖场景种子；None 时使用场景自带 seed。
        requests_per_worker: 每个 worker 的请求数。

    返回:
        LoadScenarioResult。
    """
    scenario.validate()
    pool = messages if messages is not None else load_messages(_DEFAULT_MESSAGES)
    effective_seed = scenario.seed if seed is None else seed
    results: list[LevelStats] = []
    aborted = None
    scenario_started = time.perf_counter()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    # base_url 必须提供：相对路径 + cookie 解析需要绝对 URL（httpx 的限制）
    async with httpx.AsyncClient(
        transport=transport, base_url="http://load", timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        # 预热：场景开始时 Router / Guardrails / 数据源仍是懒加载，
        # 首个请求会额外付出一次性初始化成本（数百毫秒）。
        # 预热请求不计入指标，避免冷启动噪声压过缓存差异。
        for index in range(min(5, len(pool))):
            await _request_worker(
                client,
                scenario.target_path,
                f"load-warmup-{scenario.name}-{index}",
                pool[index].message,
            )
        # 清掉预热写入的缓存：保证被测阶段从空缓存开始，命中率只反映测量阶段
        _clear_eval_cache(environment)

        for concurrency in scenario.concurrency_levels:
            count = resolve_requests(scenario, concurrency, requests_per_worker)
            # 并发度预热：先并发打满一次线程池再开始计时，避免线程池扩容成本
            # 被算进前几十个请求的延迟。预热请求不计入指标，也不写缓存。
            await asyncio.gather(
                *(
                    _request_worker(
                        client,
                        scenario.target_path,
                        f"load-warmup-{scenario.name}-{concurrency}-{index}",
                        pool[index % len(pool)].message,
                    )
                    for index in range(concurrency)
                )
            )
            _clear_eval_cache(environment)
            # 每个并发度用 seed+concurrency 派生序列：可复现，且不同并发度不共用同一序列
            sequence = build_request_sequence(
                pool,
                count=count,
                repeat_ratio=scenario.repeat_ratio,
                seed=effective_seed + concurrency,
            )
            if progress:
                progress(
                    f"[{scenario.name}] 并发 {concurrency}，请求 {count}，"
                    f"路径 {scenario.target_path}"
                )

            semaphore = asyncio.Semaphore(concurrency)

            async def _one(index: int, item: LoadMessage) -> RequestRecord:
                """获取信号量后执行一次请求；session_id 每请求独立。"""
                async with semaphore:
                    session_id = f"load-{scenario.name}-{concurrency}-{index}"
                    return await _request_worker(
                        client, scenario.target_path, session_id, item.message
                    )

            records = await asyncio.gather(
                *(_one(index, item) for index, item in enumerate(sequence))
            )
            stats = aggregate_level(records, concurrency)
            results.append(stats)

            # 错误率超过阈值：记录中止原因并停止后续并发度，避免把故障当性能
            if stats.error_rate > ABORT_ERROR_RATE:
                aborted = "error_rate_exceeded"
                break

    return LoadScenarioResult(
        scenario=scenario,
        levels=results,
        wall_clock_ms=round((time.perf_counter() - scenario_started) * 1000.0, 2),
        aborted=aborted,
        mode=getattr(environment, "mode", "mock"),
        seed=effective_seed,
        environment=_environment_snapshot(environment),
    )


def run_scenarios(
    scenarios: list[LoadScenario],
    *,
    mode: str = "mock",
    seed: int = 42,
    requests_per_worker: int = 25,
    messages: list[LoadMessage] | None = None,
    progress=None,
) -> list[LoadScenarioResult]:
    """在统一隔离环境中顺序执行多个场景。

    参数:
        scenarios: 场景列表。
        mode: "mock" / "real"。
        seed: 随机种子。
        requests_per_worker: 每个 worker 的请求数。
        messages: 消息池；None 时读取默认文件。
        progress: 进度回调。

    返回:
        LoadScenarioResult 列表。

    异常:
        RuntimeError: mode=real 但未配置 API Key。
    """
    if mode == "real" and not _has_api_key():
        raise RuntimeError("mode=real 但未配置 DEEPSEEK_API_KEY / OPENAI_API_KEY")

    pool = messages if messages is not None else load_messages(_DEFAULT_MESSAGES)
    results: list[LoadScenarioResult] = []

    with _EvalEnvironment(mode) as environment:
        # Mock 模式的 bot 没有真实推理耗时；注入极小延迟以代表编排成本，
        # 否则缓存收益会被 ASGI/线程池开销完全淹没（见常量注释）。
        if mode == "mock":
            app_module.bot = _LatencyBot(
                app_module.bot, MOCK_LLM_ORCHESTRATION_MS
            )
        # 用线程安全内存会话库替换临时 SQLite：避免并发写锁竞争污染指标
        app_module.store = _ConcurrentSessionStore()
        base_settings = app_module.settings

        async def _main():
            for scenario in scenarios:
                _prepare_scenario(environment, base_settings, scenario)
                results.append(
                    await run_scenario(
                        scenario,
                        environment=environment,
                        collector=EvalCollector(),
                        progress=progress,
                        messages=pool,
                        seed=seed,
                        requests_per_worker=requests_per_worker,
                    )
                )

        # 压测期间关闭进程内限流：避免 429 把部署策略误读为业务错误
        rate_limits = _disable_rate_limit()
        log_state = _silence_info_logs()
        try:
            asyncio.run(_main())
        finally:
            _restore_log_level(log_state)
            _restore_rate_limit(rate_limits)

    return results


def run_load(
    scenario_names=DEFAULT_SCENARIO_NAMES,
    *,
    mode: str = "mock",
    seed: int = 42,
    requests_per_worker: int = 25,
    out_dir: str | Path | None = None,
    progress=None,
) -> list[LoadScenarioResult]:
    """执行压测并写出 JSON / Markdown 报告。

    参数:
        scenario_names: 场景名序列（"all" 表示全部）或 LoadScenario 列表。
        mode: "mock"（默认）/ "real"。
        seed: 随机种子。
        requests_per_worker: 每个 worker 的请求数。
        out_dir: 报告目录；None 时写入 reports/。
        progress: 进度回调。

    返回:
        LoadScenarioResult 列表。
    """
    values = list(scenario_names or ())
    if values and all(isinstance(item, LoadScenario) for item in values):
        # CLI 可能已对场景做过并发度覆盖，直接复用对象
        scenarios = values
    else:
        scenarios = select_scenarios(values)
    if not scenarios:
        raise ValueError("至少需要选择一个压测场景")

    started_at = datetime.now().isoformat(timespec="seconds")
    results = run_scenarios(
        scenarios,
        mode=mode,
        seed=seed,
        requests_per_worker=requests_per_worker,
        progress=progress,
    )
    finished_at = datetime.now().isoformat(timespec="seconds")

    write_json_report(
        results,
        mode=mode,
        seed=seed,
        started_at=started_at,
        finished_at=finished_at,
        out_dir=out_dir,
    )
    write_markdown_report(
        results,
        mode=mode,
        seed=seed,
        started_at=started_at,
        finished_at=finished_at,
        out_dir=out_dir,
    )
    return results


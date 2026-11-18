"""本地评测 runner：读取固定任务集，逐条执行并产出 JSON / Markdown 报告。

执行方式:
    通过 httpx ASGITransport 调用 `/chat`（而不是直接调 `bot.reply`），
    因为 `route_selected`、`cache_*` 等指标都产生在 app 层；这样无需复制
    app 的路由 / 缓存编排逻辑，同时 `bot.reply` 仍会被真实执行。

隔离原则:
    - 会话库：使用临时 SQLite；
    - pending：强制内存后端，评测结束恢复；
    - 数据源：使用 data/*.json 的内存副本，写操作不落盘，保证可重复运行；
    - Mock 模式额外安装进程内 FakeRedis，让缓存路径可确定性覆盖
      （清空 FakeRedis 的字典，不使用 Redis FLUSHDB）。

为什么单线程串行:
    并发评测会引入排队延迟与资源竞争，P95 不可解释；
    串行 + 多次重跑的 P95 更接近真实的单请求尾延迟。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import redis as redis_module

from agent_system import app as app_module
from agent_system import tools
from agent_system.agent import LLMClient, MockLLM
from agent_system.app import app
from agent_system.cache import (
    get_nmsg_cache,
    reset_nmsg_cache,
    reset_redis_manager,
    reset_session_state_cache,
)
from agent_system.config import PROJECT_ROOT, get_settings
from agent_system.datasource import DATA_DIR, JsonDataSource
from agent_system.errors.exceptions import LLMError
from agent_system.eval.collector import EvalCollector
from agent_system.eval.langfuse_eval import upload_dataset, upload_scores
from agent_system.eval.metrics import EvalReport, compute_report, compute_task_result
from agent_system.eval.reporter import write_json, write_markdown
from agent_system.eval.task_schema import TaskCase, load_tasks
from agent_system.harness.builtin_profiles import register_builtin_profiles
from agent_system.harness.builtin_tools import register_all as register_builtin_tools
from agent_system.harness.guardrails import get_guardrails, reset_guardrails
from agent_system.harness.pending_store import reset_pending_store
from agent_system.harness.router import get_router, reset_router
from agent_system.memory import SessionStore

_DEFAULT_TASKS = PROJECT_ROOT / "evals" / "tasks.jsonl"


class _FakeRedis:
    """评测用进程内 Redis 替身：只实现缓存路径用到的最小命令。

    为什么不连真实 Redis:
        离线评测不应依赖外部服务；FakeRedis 让“命中/未命中”可确定复现，
        清空时只清自己的字典，绝不触碰任何真实实例的数据。
    """

    def __init__(self) -> None:
        """初始化空数据；不建立任何连接。"""
        self.data: dict[str, object] = {}

    def ping(self) -> bool:
        """健康检查。"""
        return True

    def close(self) -> None:
        """兼容 redis-py 接口。"""
        return None

    def get(self, key):
        """读取字符串值。"""
        value = self.data.get(key)
        return value if isinstance(value, str) else None

    def setex(self, key, ttl, value):
        """写入带过期时间的字符串（FakeRedis 不真正过期）。"""
        self.data[key] = value
        return True

    def delete(self, key):
        """删除 key。"""
        self.data.pop(key, None)
        return 1

    def hset(self, key, mapping=None, **kwargs):
        """写入 HASH 字段；供会话状态缓存使用。"""
        current = self.data.get(key)
        if not isinstance(current, dict):
            current = {}
        current.update(dict(mapping or {}))
        current.update(kwargs)
        self.data[key] = current
        return len(current)

    def hgetall(self, key):
        """读取 HASH 全部字段。"""
        current = self.data.get(key)
        return dict(current) if isinstance(current, dict) else {}

    def expire(self, key, ttl):
        """FakeRedis 不实现 TTL，返回 True 保持接口兼容。"""
        return True

    def clear(self) -> None:
        """清空本次评测产生的数据（仅内存字典）。"""
        self.data.clear()


class _InMemoryDataSource(JsonDataSource):
    """评测用数据源：把 data/*.json 复制进内存，写操作只改内存副本。

    这样 `apply_refund` / `cancel_order` 不会修改仓库里的 data/orders.json，
    同一份任务集可以反复运行且结果稳定。
    """

    def __init__(self) -> None:
        """初始化空的文件缓存。"""
        self._files: dict[str, list] = {}

    def _load(self, filename: str) -> list:
        """首次读取时从磁盘加载，之后只返回内存副本。"""
        if filename not in self._files:
            path = DATA_DIR / filename
            if path.exists():
                try:
                    self._files[filename] = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    self._files[filename] = []
            else:
                self._files[filename] = []
        return copy.deepcopy(self._files[filename])

    def _save(self, filename: str, data: list) -> None:
        """写操作只更新内存，不落盘。"""
        self._files[filename] = copy.deepcopy(data)


class _FaultyDataSource:
    """工具异常注入：被指定的方法调用时抛异常，其余方法委托给真实数据源。"""

    def __init__(self, source, tool_name: str):
        """保存原始数据源与需要制造故障的工具名。"""
        self._source = source
        self._tool = tool_name

    def __getattr__(self, name):
        """目标方法返回抛异常的桩，其余属性透传。"""
        if name == self._tool:
            def _fail(*args, **kwargs):
                raise RuntimeError(f"eval injected tool failure: {name}")

            return _fail
        return getattr(self._source, name)


class _FailingBot:
    """LLM 异常注入：模拟上游超时。"""

    _last_used_tools: list[str] = []

    def reply(self, message, history=None, *, profile=None):
        """抛出可重试的 LLM 领域异常（映射为 llm_failed）。"""
        raise LLMError(retryable=True, cause=TimeoutError("eval injected llm timeout"))

    def reply_stream(self, message, history=None, *, profile=None):
        """流式路径同样抛出异常。"""
        raise LLMError(retryable=True, cause=TimeoutError("eval injected llm timeout"))


class _EvalEnvironment:
    """一次评测运行的隔离环境：临时存储、内存数据源、Mock 缓存。

    使用方式:
        with _EvalEnvironment(mode) as env:
            ...
    """

    def __init__(self, mode: str):
        """记录模式；真正的替换在 __enter__ 中完成。"""
        self.mode = mode
        self.fake_redis: _FakeRedis | None = None
        self._tmpdir: tempfile.TemporaryDirectory | None = None

    def __enter__(self):
        """保存并替换全局状态，返回自身。"""
        self._saved_bot = app_module.bot
        self._saved_settings = app_module.settings
        self._saved_cache = app_module.nmsg_cache
        self._saved_store = app_module.store
        self._saved_source = tools._source
        self._saved_redis_url = os.environ.get("REDIS_URL")
        self._saved_from_url = redis_module.from_url
        self._saved_pending_backend = os.environ.get("PENDING_STORE_BACKEND")

        # 会话库：临时文件，评测结束即删除
        self._tmpdir = tempfile.TemporaryDirectory()
        self._temp_store = SessionStore(
            db_path=str(Path(self._tmpdir.name) / "sessions.db")
        )
        app_module.store = self._temp_store

        # 数据源：内存副本，写操作不落盘
        tools._source = _InMemoryDataSource()

        # pending：强制内存后端，避免误用真实 Redis
        os.environ["PENDING_STORE_BACKEND"] = "memory"
        get_settings.cache_clear()
        reset_pending_store()
        reset_guardrails()
        reset_router()

        # bot：mock 模式强制 MockLLM；real 模式确保使用 LLMClient
        if self.mode == "mock":
            app_module.bot = MockLLM()
        elif not isinstance(app_module.bot, LLMClient):
            app_module.bot = LLMClient()

        # 缓存：仅 mock 模式安装 FakeRedis，让命中路径可复现
        if self.mode == "mock":
            self.fake_redis = _FakeRedis()
            redis_module.from_url = lambda *args, **kwargs: self.fake_redis
            os.environ["REDIS_URL"] = "redis://eval-fake:6379/0"
            get_settings.cache_clear()
            reset_redis_manager()
            reset_nmsg_cache()
            reset_session_state_cache()
            app_module.nmsg_cache = get_nmsg_cache()
            app_module.settings = replace(
                app_module.settings, nmsg_cache_enabled=True
            )
        return self

    def clear_per_task(self) -> None:
        """任务开始前清空本次运行的缓存数据（仅 FakeRedis）。"""
        if self.fake_redis is not None:
            self.fake_redis.clear()

    def __exit__(self, exc_type, exc, tb):
        """恢复全部全局状态；某个清理步骤失败不阻断其它清理。"""
        app_module.bot = self._saved_bot
        app_module.settings = self._saved_settings
        app_module.nmsg_cache = self._saved_cache
        app_module.store = self._saved_store
        tools._source = self._saved_source
        redis_module.from_url = self._saved_from_url

        if self._saved_redis_url is None:
            os.environ.pop("REDIS_URL", None)
        else:
            os.environ["REDIS_URL"] = self._saved_redis_url
        if self._saved_pending_backend is None:
            os.environ.pop("PENDING_STORE_BACKEND", None)
        else:
            os.environ["PENDING_STORE_BACKEND"] = self._saved_pending_backend

        get_settings.cache_clear()
        reset_redis_manager()
        reset_nmsg_cache()
        reset_session_state_cache()
        reset_pending_store()
        reset_guardrails()
        reset_router()

        # Windows 下必须先关闭临时 SQLite 连接，否则临时目录无法删除
        connection = getattr(self._temp_store, "_conn", None)
        if connection is not None:
            connection.close()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
        return False


@contextmanager
def _apply_injections(task: TaskCase):
    """按任务配置临时注入故障，退出时恢复。

    参数:
        task: 当前任务。

    说明:
        所有注入都只改评测进程内的引用，不改业务代码；
        恢复动作按注入顺序逆序执行，保证嵌套注入也能回到初始状态。
    """
    inject = task.inject or {}
    kind = inject.get("type")
    restorers = []

    if kind == "tool_error":
        original = tools._source
        tools._source = _FaultyDataSource(original, inject.get("tool", "query_order"))
        restorers.append(lambda: setattr(tools, "_source", original))
    elif kind == "llm_error":
        original_bot = app_module.bot
        app_module.bot = _FailingBot()
        restorers.append(lambda: setattr(app_module, "bot", original_bot))
    elif kind == "route_exception":
        router = get_router()
        original_route = router.route

        def _raise_route(message):
            raise RuntimeError("eval injected route failure")

        router.route = _raise_route
        restorers.append(lambda: setattr(router, "route", original_route))
    elif kind == "confirmation_ttl":
        guardrails = get_guardrails()
        original_ttl = guardrails.ttl_seconds
        guardrails.ttl_seconds = int(inject.get("value", 0))
        restorers.append(lambda: setattr(guardrails, "ttl_seconds", original_ttl))

    try:
        yield
    finally:
        for restore in reversed(restorers):
            restore()


def _resolve_mode(mode: str) -> str:
    """把 auto/mock/real 解析成实际运行模式。

    参数:
        mode: "auto" / "mock" / "real"。

    返回:
        "mock" 或 "real"。

    异常:
        ValueError: 未知模式。
        RuntimeError: 强制 real 但未配置 API Key。
    """
    if mode not in {"auto", "mock", "real"}:
        raise ValueError(f"未知评测模式: {mode!r}")
    has_key = bool(
        os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    if mode == "mock":
        return "mock"
    if mode == "real":
        if not has_key:
            raise RuntimeError(
                "mode=real 但未配置 DEEPSEEK_API_KEY / OPENAI_API_KEY"
            )
        return "real"
    return "real" if has_key else "mock"


def _reply_of(response) -> str:
    """从 /chat 响应中提取 reply 文本；错误响应返回空串。"""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("reply"), str):
        return payload["reply"]
    return ""


def run(
    tasks_path: str | Path = _DEFAULT_TASKS,
    *,
    mode: str = "auto",
    reports_dir: str | Path | None = None,
) -> EvalReport:
    """执行一次完整评测。

    参数:
        tasks_path: 任务集 JSONL 路径。
        mode: "auto"（有 Key 走真实 LLM，否则 Mock）/ "mock" / "real"。
        reports_dir: 报告输出目录；None 时写入项目 reports/。

    返回:
        EvalReport。

    异常:
        RuntimeError: mode=real 但未配置 API Key。
        ValueError: 任务集格式非法。
    """
    tasks = load_tasks(tasks_path)
    resolved = _resolve_mode(mode)

    # Profile 依赖 Tool Registry，先保证注册（幂等）
    register_builtin_tools()
    register_builtin_profiles()

    started_at = datetime.now().isoformat(timespec="seconds")
    with _EvalEnvironment(resolved) as env:
        with EvalCollector().install() as collector:
            results, skipped, cache_hits, cache_misses, trace_ids = _run_batch(
                env, tasks, resolved, collector
            )
    finished_at = datetime.now().isoformat(timespec="seconds")

    report = compute_report(
        mode=resolved,
        seed=get_settings().eval_seed,
        started_at=started_at,
        finished_at=finished_at,
        results=results,
        skipped_tasks=skipped,
        cache_hit_count=cache_hits,
        cache_miss_count=cache_misses,
    )

    # Langfuse 未启用时两个函数立即返回；这里再兜一层，上传绝不影响评测
    try:
        upload_dataset(tasks)
        upload_scores(report, trace_ids)
    except Exception:  # noqa: BLE001 - 上传是旁路能力
        pass

    write_json(report, reports_dir)
    write_markdown(report, reports_dir)
    return report


def _run_batch(env: _EvalEnvironment, tasks, mode: str, collector: EvalCollector):
    """在隔离环境中串行执行任务，并在每个任务前清理缓存。

    参数:
        env: 已进入的隔离环境。
        tasks: TaskCase 列表。
        mode: 实际模式。
        collector: 已安装的采集器。

    返回:
        (results, skipped, cache_hits, cache_misses, trace_ids)。
    """
    results = []
    skipped = []
    cache_hits = 0
    cache_misses = 0
    trace_ids: dict[str, str] = {}
    settings = get_settings()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async def _execute_one(task: TaskCase):
        nonlocal cache_hits, cache_misses
        env.clear_per_task()
        collector.start_task(task.id)
        started = time.perf_counter()
        reply = ""
        last_request_id = ""
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://eval"
            ) as client:
                with _apply_injections(task):
                    for index, message in enumerate(task.messages):
                        last_request_id = f"eval-{task.id}-{index:02d}"
                        response = await client.post(
                            "/chat",
                            json={"session_id": task.session_id, "message": message},
                            headers={"X-Request-ID": last_request_id},
                        )
                        reply = _reply_of(response)
        finally:
            latency_ms = (time.perf_counter() - started) * 1000
            snapshot = collector.stop_task() or {}

        results.append(
            compute_task_result(
                task=task,
                snapshot=snapshot,
                reply=reply,
                latency_ms=latency_ms,
                input_price_per_1k=settings.eval_input_price_per_1k_tokens,
                output_price_per_1k=settings.eval_output_price_per_1k_tokens,
            )
        )
        cache_hits += int(snapshot.get("cache_hit") or 0)
        cache_misses += int(snapshot.get("cache_miss") or 0)
        if last_request_id:
            trace_ids[task.id] = last_request_id

    async def _main():
        for task in tasks:
            if task.only_real_llm and mode == "mock":
                skipped.append(
                    {
                        "task_id": task.id,
                        "category": task.category,
                        "reason": "仅真实 LLM 模式运行",
                        "notes": task.notes,
                    }
                )
                continue
            await _execute_one(task)

    asyncio.run(_main())
    return results, skipped, cache_hits, cache_misses, trace_ids


def main(argv=None) -> int:
    """命令行入口：执行评测并打印简要汇总。

    参数:
        argv: 可选参数列表（默认读取 sys.argv）。

    返回:
        进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description="固定任务集离线评测器")
    parser.add_argument("--tasks", default=str(_DEFAULT_TASKS), help="任务集 JSONL 路径")
    parser.add_argument(
        "--mode",
        choices=("auto", "mock", "real"),
        default="auto",
        help="运行模式；auto 会根据 API Key 自动选择",
    )
    parser.add_argument("--reports-dir", default=None, help="报告输出目录")
    args = parser.parse_args(argv)

    report = run(args.tasks, mode=args.mode, reports_dir=args.reports_dir)

    print(
        json.dumps(
            {
                "mode": report.mode,
                "total": report.total,
                "executed": report.executed,
                "passed": report.passed,
                "skipped": report.skipped,
                "pass_rate": report.pass_rate,
                "p50_ms": report.latency.p50_ms,
                "p95_ms": report.latency.p95_ms,
                "cache_hit_rate": report.cache_hit_rate,
                "cost_usd_total": report.cost_usd_total,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

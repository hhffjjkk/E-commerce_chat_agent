"""评测事件采集器：把一次任务执行中的结构化事件归集成指标。

为什么不在生产代码里加钩子:
    评测是离线任务，生产代码不应感知评测；本模块只在 runner 内部临时替换
    `log_event`，执行完成后立即恢复，不影响同进程中的其它调用。

为什么不能只 patch `obs.events.log_event`:
    项目内多数模块使用 `from agent_system.obs.events import log_event` 直接绑定
    函数对象，仅替换 events 模块属性不会影响这些已绑定的引用。因此 install()
    会同时替换所有已加载 agent_system 模块中指向原函数的属性，并在退出时逐一恢复。

线程安全:
    评测是单线程串行执行（并发会引入排队延迟，P95 不可解释），因此不需要锁。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager

from agent_system.obs.request_id import get_request_id, get_session_id

# 需要派生计数的事件名，集中在此便于核对口径
_ROUTE_EVENT = "route_selected"
_LLM_EVENT = "llm_call"
_TOOL_EVENT = "tool_call"
_CACHE_HIT = "cache_hit"
_CACHE_MISS = "cache_miss"
_CACHE_WRITE = "cache_write"
_CACHE_WRITE_SKIPPED = "cache_write_skipped"
_CONFIRM_EVENTS = (
    "confirmation_requested",
    "confirmation_granted",
    "confirmation_rejected",
    "confirmation_expired",
    "confirmation_failed",
)
_ERROR_EVENTS = ("error_raised", "request_error")


def _payload_of(event: str, fields: dict) -> dict:
    """按 log_event 的口径补全 request_id / session_id，生成采集用字典。

    参数:
        event: 事件名。
        fields: 事件字段（调用方传入的副本）。

    返回:
        与日志 JSON 结构一致的事件字典。

    说明:
        这里只复制 log_event 的“补上下文”逻辑，不复制日志输出；
        真正写日志仍由原函数完成，保证生产日志与评测采集口径一致。
    """
    data = dict(fields)
    request_id = data.pop("request_id", None) or get_request_id()
    payload = {"event": event, "request_id": request_id or "-"}
    if "session_id" not in data:
        session_id = get_session_id()
        if session_id:
            payload["session_id"] = session_id
    payload.update(data)
    return payload


class EvalCollector:
    """按 task_id 归集事件并派生任务级指标。"""

    def __init__(self) -> None:
        """初始化空采集器；不安装任何 hook。"""
        self._task_id: str | None = None
        self._events: list[dict] = []
        self._snapshots: dict[str, dict] = {}
        self._last_snapshot: dict | None = None

    @contextmanager
    def install(self):
        """临时替换全局 log_event，退出时完整恢复。

        返回:
            上下文管理器，yield 自身。

        说明:
            解析日志时不再需要像旧 runner 那样按 request_id 过滤：
            串行执行 + start_task/stop_task 边界已经保证事件归属。
        """
        from agent_system.obs import events as events_module

        original = events_module.log_event
        patched_modules: list[tuple[object, str]] = []

        def patched(event, **fields):
            """先产出原始日志，再交给采集器；日志失败不影响采集。"""
            try:
                original(event, **fields)
            finally:
                self._record(_payload_of(event, fields))

        # 1) events 模块自身的函数（request_end 等）通过模块全局调用 log_event
        events_module.log_event = patched
        patched_modules.append((events_module, "log_event"))

        # 2) 已加载模块中直接绑定的引用
        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "")
            if not name or not name.startswith("agent_system"):
                continue
            if getattr(module, "log_event", None) is original:
                setattr(module, "log_event", patched)
                patched_modules.append((module, "log_event"))

        try:
            yield self
        finally:
            # 恢复所有仍指向 patched 的属性；运行期新导入的模块也一并覆盖
            for module in list(sys.modules.values()):
                if getattr(module, "log_event", None) is patched:
                    setattr(module, "log_event", original)
            for module, attr in patched_modules:
                if getattr(module, attr, None) is patched:
                    setattr(module, attr, original)

    def start_task(self, task_id: str) -> None:
        """开始采集一个任务；此前的孤立事件不会被归入本任务。"""
        self._task_id = task_id
        self._events = []

    def stop_task(self) -> dict | None:
        """结束当前任务并返回快照；没有正在采集的任务时返回 None。"""
        if self._task_id is None:
            return None
        task_id = self._task_id
        snapshot = self._build_snapshot(task_id, self._events)
        self._snapshots[task_id] = snapshot
        self._last_snapshot = snapshot
        self._task_id = None
        self._events = []
        return snapshot

    def snapshot(self) -> dict:
        """返回最近一个任务的快照；没有任务时返回空快照（便于测试）。"""
        if self._last_snapshot is not None:
            return self._last_snapshot
        return self._build_snapshot("", [])

    def task_snapshot(self, task_id: str) -> dict | None:
        """按 task_id 取历史快照；不存在返回 None。"""
        return self._snapshots.get(task_id)

    def task_summary(self, task_id: str) -> dict:
        """返回指定任务的精简摘要（供压测/报告复用）。

        参数:
            task_id: 任务标识。

        返回:
            {"cache_hit": bool, "llm_calls": int, "route_agent": str | None}。

        说明:
            找不到任务时返回中性值，不抛异常；该方法只读取已保存的快照，
            不改变已有字段与采集语义。
        """
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return {"cache_hit": False, "llm_calls": 0, "route_agent": None}
        route = snapshot.get("route") or {}
        return {
            "cache_hit": int(snapshot.get("cache_hit") or 0) > 0,
            "llm_calls": int(snapshot.get("llm_calls") or 0),
            "route_agent": route.get("agent"),
        }

    def _record(self, payload: dict) -> None:
        """记录一条事件；start_task/stop_task 之外的事件被忽略。"""
        if self._task_id is None:
            return
        self._events.append(payload)

    @staticmethod
    def _build_snapshot(task_id: str, events: list[dict]) -> dict:
        """把事件列表折叠成任务级指标。

        参数:
            task_id: 任务标识。
            events: 事件字典列表。

        返回:
            包含路由、LLM、工具、缓存、确认与错误指标的字典。

        口径:
            - llm_calls 只计 phase=start，避免与 end 重复计数；
            - token 只从 phase=end 累加（失败调用没有 usage，按 0 计）；
            - route 取最后一次 route_selected（多轮任务按最后一次请求判定）。
        """
        route = None
        llm_calls = 0
        llm_latency = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        models: list[str] = []
        tools_used: list[str] = []
        tool_latency = 0.0
        tool_ok = 0
        tool_failed = 0
        cache_hits = 0
        cache_misses = 0
        cache_writes = 0
        cache_write_skipped = 0
        skip_reasons: list[str] = []
        confirmation = {name: 0 for name in _CONFIRM_EVENTS}
        errors: list[str] = []
        request_ids: list[str] = []

        for event in events:
            name = event.get("event")
            request_id = event.get("request_id")
            if request_id and request_id != "-" and request_id not in request_ids:
                request_ids.append(request_id)

            if name == _ROUTE_EVENT:
                route = {
                    "agent": event.get("agent"),
                    "strategy": event.get("strategy"),
                    "confidence": event.get("confidence"),
                    "reason": event.get("reason"),
                }
            elif name == _LLM_EVENT:
                if event.get("phase") == "start":
                    llm_calls += 1
                    model = event.get("model")
                    if model and model not in models:
                        models.append(model)
                else:
                    llm_latency += float(event.get("latency_ms") or 0.0)
                    prompt_tokens += int(event.get("prompt_tokens") or 0)
                    completion_tokens += int(event.get("completion_tokens") or 0)
            elif name == _TOOL_EVENT:
                tool = event.get("tool")
                if tool:
                    tools_used.append(tool)
                tool_latency += float(event.get("latency_ms") or 0.0)
                if event.get("ok") is True:
                    tool_ok += 1
                else:
                    tool_failed += 1
            elif name == _CACHE_HIT:
                cache_hits += 1
            elif name == _CACHE_MISS:
                cache_misses += 1
            elif name == _CACHE_WRITE:
                cache_writes += 1
            elif name == _CACHE_WRITE_SKIPPED:
                cache_write_skipped += 1
                reason = event.get("reason")
                if reason:
                    skip_reasons.append(str(reason))
            elif name in _CONFIRM_EVENTS:
                confirmation[name] += 1
            elif name in _ERROR_EVENTS:
                code = event.get("code") if name == "error_raised" else "request_error"
                errors.append(str(code or "unknown_error"))

        return {
            "task_id": task_id,
            "events": list(events),
            "route": route,
            "llm_calls": llm_calls,
            "llm_latency_ms": round(llm_latency, 2),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "models": models,
            "tools_used": tools_used,
            "tool_latency_ms": round(tool_latency, 2),
            "tool_ok": tool_ok,
            "tool_failed": tool_failed,
            "cache_hit": cache_hits,
            "cache_miss": cache_misses,
            "cache_write": cache_writes,
            "cache_write_skipped": cache_write_skipped,
            "cache_write_skipped_reasons": skip_reasons,
            "confirmation": confirmation,
            "errors": errors,
            "request_ids": request_ids,
        }

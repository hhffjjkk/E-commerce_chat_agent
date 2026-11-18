"""NDJSON 流式事件发射器。

本模块位于协议层：把一次 `/chat/stream` 请求的事件（start / error / done 等）
编码为单行 JSON，并负责在最外层包装业务生成器，保证向后兼容。
本步骤只提供字段与发射能力，不填充路由 / 工具轨迹 / 用量等业务数据。
"""

import json
import time
from typing import Literal

from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError
from agent_system.obs.request_id import get_request_id
from agent_system.stream.schema import STREAM_PROTOCOL_VERSION

# done 事件可回填的“已发射摘要”键
_SUMMARY_KEYS = ("route", "usage", "cache")


def _encode_event(event: dict) -> str:
    """把事件字典编码为 NDJSON 单行（含结尾换行符）。

    参数:
        event: 事件字典，必须可被 json 序列化。

    返回:
        形如 '{"event": "start", ...}\\n' 的字符串，可直接写入流。
    """
    return json.dumps(event, ensure_ascii=False) + "\n"


def _line_is_done_event(line) -> bool:
    """判断生成器输出的一行是否已是扩展 done 事件。

    参数:
        line: 生成器产出的单行内容，可能是 str 或 bytes。

    返回:
        若该行是 event == "done" 的 JSON 对象则返回 True，否则 False。
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(line, str):
        return False
    text = line.strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("event") == "done"


class StreamEmitter:
    """NDJSON 事件发射器：统一补齐 request_id / ts / seq，并维护协议状态。

    生命周期：
        每个 `/chat/stream` 请求构造一个实例；首个事件应为 `emit_start`，
        `emit_done` 之后实例关闭，再发射任何事件都会抛 RuntimeError。

    并发假设：
        单实例只在单个生成器迭代中使用，不做跨线程共享；内部状态无锁。
    """

    def __init__(
        self,
        request_id: str | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        trace_id_provider=None,
    ):
        """初始化发射器。

        参数:
            request_id: 显式请求 ID；为空时从步骤 1 的 contextvars 读取。
            session_id: 会话 ID，可显式传入或后续在 emit_start 时指定。
            trace_id: Langfuse trace id，为空时由 provider 解析；都为空则不输出。
            span_id: Langfuse span id，为空时事件不输出该字段。
            trace_id_provider: 可选回调，在 emit_start / emit_done 时惰性解析 trace id；
                结果会缓存到实例上，后续不再重复调用。
        """
        self._request_id = request_id or get_request_id() or "-"
        self._session_id = session_id
        self._trace_id = trace_id
        self._span_id = span_id
        self._trace_id_provider = trace_id_provider
        self._seq = 0
        self._closed = False
        self._collected: dict[str, dict] = {}
        self._started_at = time.perf_counter()

    def _resolve_trace_id(self) -> str | None:
        """惰性解析并缓存 trace id。

        返回:
            当前 trace id；未配置或 provider 失败时返回 None。

        说明:
            仅在 start / done 事件发射前调用；provider 异常不影响流式输出。
        """
        if self._trace_id is None and self._trace_id_provider is not None:
            try:
                self._trace_id = self._trace_id_provider()
            except Exception:  # noqa: BLE001 - 观测层异常不能中断协议输出
                self._trace_id = None
        return self._trace_id

    @property
    def request_id(self) -> str:
        """返回构造时捕获的 request_id（供包装器复用，避免再次读取 contextvars）。"""
        return self._request_id

    @property
    def closed(self) -> bool:
        """返回发射器是否已关闭（done 已发射）。"""
        return self._closed

    def _ensure_open(self) -> None:
        """断言发射器仍处于可写状态。

        异常:
            RuntimeError: 若 done 已发射，禁止继续写入以保证协议顺序。
        """
        if self._closed:
            raise RuntimeError("stream emitter 已关闭：done 之后不允许继续发射事件")

    def _base(self, event: str) -> dict:
        """构造所有事件共用的基础字段并自增序号。

        参数:
            event: 事件名，如 "start"。

        返回:
            含 event / request_id / ts / seq 的字典；trace_id、span_id 仅在已设置时附加。
        """
        payload = {
            "event": event,
            "request_id": self._request_id,
            "ts": int(time.time() * 1000),
            "seq": self._seq,
        }
        self._seq += 1
        # Langfuse 预留：未注入上下文时不输出空值，避免污染 JSON
        if self._trace_id is not None:
            payload["trace_id"] = self._trace_id
        if self._span_id is not None:
            payload["span_id"] = self._span_id
        return payload

    def emit_start(self, session_id: str | None = None) -> dict:
        """发射流开始事件。

        参数:
            session_id: 会话 ID；为空时使用构造时的 session_id。

        返回:
            start 事件字典，含 protocol_version。
        """
        self._ensure_open()
        # start/done 是回填 trace_id 的唯一两个事件：在 _base 之前解析并缓存
        self._resolve_trace_id()
        payload = self._base("start")
        # 协议要求 start 必带 session_id，缺失时用空串占位而不是 null
        payload["session_id"] = session_id or self._session_id or ""
        payload["protocol_version"] = STREAM_PROTOCOL_VERSION
        return payload

    def emit_route(
        self,
        agent: str,
        confidence: float | None = None,
        reason: str | None = None,
        *,
        display_name: str | None = None,
        strategy: str | None = None,
    ) -> dict:
        """发射路由结果事件。

        参数:
            agent: 命中的 Agent 名称。
            confidence: 路由置信度，可为空。
            reason: 路由原因标签，可为空。
            display_name: 子 Agent 中文展示名（可选字段，阶段四接入新增）。
            strategy: 路由策略（rule / llm / fallback / pending_override，可选字段）。

        返回:
            route 事件字典。

        说明:
            新增的 display_name / strategy 均为可选字段，不改变既有必填字段，
            旧前端忽略它们即可。
        """
        self._ensure_open()
        payload = self._base("route")
        payload["agent"] = agent
        if confidence is not None:
            payload["confidence"] = confidence
        if reason is not None:
            payload["reason"] = reason
        if display_name is not None:
            payload["display_name"] = display_name
        if strategy is not None:
            payload["strategy"] = strategy
        # done 事件只回填摘要，不重复完整业务数据
        summary = {"agent": agent}
        if confidence is not None:
            summary["confidence"] = confidence
        if strategy is not None:
            summary["strategy"] = strategy
        self._collected["route"] = summary
        return payload

    def emit_tool_call(
        self,
        tool: str,
        ok: bool,
        latency_ms: float,
        args_keys: list[str] | None = None,
    ) -> dict:
        """发射工具调用轨迹事件（阶段三填充真实数据）。

        参数:
            tool: 工具名。
            ok: 是否执行成功。
            latency_ms: 工具耗时（毫秒）。
            args_keys: 参数名列表（只记键名，不记值）。

        返回:
            tool_call 事件字典。
        """
        self._ensure_open()
        payload = self._base("tool_call")
        payload["tool"] = tool
        payload["ok"] = ok
        payload["latency_ms"] = round(latency_ms, 2)
        if args_keys is not None:
            payload["args_keys"] = list(args_keys)
        return payload

    def emit_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None = None,
    ) -> dict:
        """发射 Token / 成本事件（阶段三填充真实数据）。

        参数:
            prompt_tokens: 输入 token 数。
            completion_tokens: 输出 token 数。
            cost_usd: 估算成本（美元），可为空。

        返回:
            usage 事件字典，含 total_tokens。
        """
        self._ensure_open()
        payload = self._base("usage")
        payload["prompt_tokens"] = prompt_tokens
        payload["completion_tokens"] = completion_tokens
        payload["total_tokens"] = prompt_tokens + completion_tokens
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        summary = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        if cost_usd is not None:
            summary["cost_usd"] = cost_usd
        self._collected["usage"] = summary
        return payload

    def emit_cache(
        self,
        hit: bool,
        layer: Literal["session", "semantic", "nmsg"],
        key_prefix: str | None = None,
    ) -> dict:
        """发射缓存命中事件（阶段五填充真实数据）。

        参数:
            hit: 是否命中缓存。
            layer: 缓存层，session / semantic / nmsg（归一化消息缓存）。
            key_prefix: 缓存键前缀，可为空；协议层默认不传，避免泄露隔离维度。

        返回:
            cache 事件字典。
        """
        self._ensure_open()
        payload = self._base("cache")
        payload["hit"] = hit
        payload["layer"] = layer
        if key_prefix is not None:
            payload["key_prefix"] = key_prefix
        self._collected["cache"] = {"hit": hit, "layer": layer}
        return payload

    def emit_error(self, error: AgentError) -> dict:
        """把领域异常摊平为 error 事件（不输出 cause / 堆栈）。

        参数:
            error: 待转换的领域异常，内部复用其 to_payload()。

        返回:
            error 事件字典，含 code / message / retryable / details。
        """
        self._ensure_open()
        payload = self._base("error")
        # to_payload 只包含可安全外露字段；cause 仅用于日志，不进入事件
        detail = error.to_payload(self._request_id)["error"]
        payload["code"] = detail["code"]
        payload["message"] = detail["message"]
        payload["retryable"] = detail["retryable"]
        payload["details"] = detail.get("details") or {}
        return payload

    def emit_done(
        self,
        status: Literal["ok", "failed"] = "ok",
        latency_ms: float | None = None,
    ) -> dict:
        """发射流结束事件并关闭发射器。

        参数:
            status: 结束状态，ok 或 failed。
            latency_ms: 本次流耗时（毫秒），可为空。

        返回:
            done 事件字典；若此前发射过 route / usage / cache，会回填对应摘要。
        """
        self._ensure_open()
        # 与 start 使用同一个已缓存的 trace_id，保证一次流只有一条 trace
        self._resolve_trace_id()
        payload = self._base("done")
        payload["status"] = status
        if latency_ms is not None:
            payload["latency_ms"] = round(latency_ms, 2)
        # 回填“已发射摘要”，便于前端在一个事件内拿到最终状态
        for key in _SUMMARY_KEYS:
            if key in self._collected:
                payload[key] = self._collected[key]
        self._closed = True
        return payload


def wrap_streaming_generator(gen, emitter: StreamEmitter):
    """包装业务流式生成器，注入 start / error / done 协议事件。

    参数:
        gen: 原始生成器，逐行产出 `{"delta": ...}`、`{"done": true}` 等行。
        emitter: 本请求对应的 StreamEmitter。

    返回:
        同步生成器，逐行产出字符串；原有行原样透传，不被改写。

    异常:
        RuntimeError: 若 emitter 已被外部关闭（正常包装流程不会触发）。

    说明:
        非 AgentError 的未捕获异常会被包装为 internal_error 后以 error 事件下发，
        这是步骤 2“流开始后异常无法改写 body”的协议级承接。
    """
    start_emitted = False
    done_emitted = False
    saw_done_event = False
    started = time.perf_counter()

    try:
        # start 必须在原始生成器首次迭代之前发出
        yield _encode_event(emitter.emit_start())
        start_emitted = True

        for line in gen:
            # 若原始生成器已自带扩展 done，则记录并原样透传，避免重复 done
            if _line_is_done_event(line):
                saw_done_event = True
            yield line

        # 原始生成器正常结束：补一条扩展 done（旧 {done: true} 行已原样透传）
        if not saw_done_event:
            latency_ms = (time.perf_counter() - started) * 1000
            yield _encode_event(emitter.emit_done("ok", latency_ms=latency_ms))
            done_emitted = True
    except AgentError as exc:
        # start 已发出 → 走协议内 error + done(failed)；否则交由 FastAPI 统一响应
        if start_emitted and not done_emitted:
            yield _encode_event(emitter.emit_error(exc))
            yield _encode_event(emitter.emit_done("failed"))
            done_emitted = True
        else:
            raise
    except Exception as exc:  # noqa: BLE001 - 流内未捕获异常统一转 internal_error
        if start_emitted and not done_emitted:
            wrapped = AgentError(ErrorCode.INTERNAL_ERROR, cause=exc)
            yield _encode_event(emitter.emit_error(wrapped))
            yield _encode_event(emitter.emit_done("failed"))
            done_emitted = True
        else:
            raise


async def wrap_async_streaming_generator(gen, emitter: StreamEmitter):
    """异步版生成器包装：用于包装 StreamingResponse 的异步 body_iterator。

    参数:
        gen: 异步可迭代对象（Starlette 会把同步生成器包成线程池异步迭代器）。
        emitter: 本请求对应的 StreamEmitter。

    返回:
        异步生成器，逐行产出字符串。

    异常:
        RuntimeError: 若 emitter 已被外部关闭（正常包装流程不会触发）。

    说明:
        与 `wrap_streaming_generator` 行为一致，仅消费方式不同；Starlette 1.x 的
        StreamingResponse 只接受异步 body_iterator，因此桥接层需要这一版本。
    """
    start_emitted = False
    done_emitted = False
    saw_done_event = False
    started = time.perf_counter()

    try:
        yield _encode_event(emitter.emit_start())
        start_emitted = True

        async for line in gen:
            if _line_is_done_event(line):
                saw_done_event = True
            yield line

        if not saw_done_event:
            latency_ms = (time.perf_counter() - started) * 1000
            yield _encode_event(emitter.emit_done("ok", latency_ms=latency_ms))
            done_emitted = True
    except AgentError as exc:
        if start_emitted and not done_emitted:
            yield _encode_event(emitter.emit_error(exc))
            yield _encode_event(emitter.emit_done("failed"))
            done_emitted = True
        else:
            raise
    except Exception as exc:  # noqa: BLE001 - 流内未捕获异常统一转 internal_error
        if start_emitted and not done_emitted:
            wrapped = AgentError(ErrorCode.INTERNAL_ERROR, cause=exc)
            yield _encode_event(emitter.emit_error(wrapped))
            yield _encode_event(emitter.emit_done("failed"))
            done_emitted = True
        else:
            raise

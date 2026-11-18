# app.py
# FastAPI 服务入口：多轮对话（含流式）、平台回调、健康检查与协议自查。
# 启动：uvicorn agent_system.app:app --host 127.0.0.1 --port 8000
#
# 本文件同时承担“装配层”职责：日志/观测初始化、错误处理器、中间件与健康路由
# 都在这里完成；不再有外部包装层或运行期 monkey-patch。

import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_system.agent import LLMClient, MockLLM, detect_escalation
from agent_system.cache import (
    RedisUnavailable,
    get_nmsg_cache,
    get_session_state_cache,
)
from agent_system.cache.nmsg_normalize import is_cacheable, normalize_message
from agent_system.channels import normalize
from agent_system.config import get_settings
from agent_system.errors.fallback import (
    NoOpFallbackHook,
    RetryObservingHook,
    register_hook,
)
from agent_system.errors.handlers import register_exception_handlers
from agent_system.harness.builtin_profiles import (
    compose_system_prompt,
    register_builtin_profiles as register_builtin_agent_profiles,
)
from agent_system.harness.context_assembler import get_context_assembler
from agent_system.harness.tool_registry import get_registry
from agent_system.harness.builtin_tools import register_all as register_builtin_tools
from agent_system.harness.pending_store import get_pending_store
from agent_system.harness.retry_policy import get_retry_policy
from agent_system.harness.router import RouteDecision
from agent_system.harness.selection import select_profile
from agent_system.memory import create_store
from agent_system.obs.events import config_loaded, log_event
from agent_system.obs.langfuse_client import init_langfuse
from agent_system.obs.langfuse_stream import current_trace_id, stream_trace_id_provider
from agent_system.obs.logging_config import configure_logging
from agent_system.obs.request_id import RequestContextMiddleware, get_request_id
from agent_system.runtime.body_limit import BodyLimitMiddleware
from agent_system.runtime.cors import configure_cors
from agent_system.runtime.health import register_health_routes
from agent_system.runtime.rate_limit import RateLimitMiddleware
from agent_system.runtime.stream_schema_route import register_stream_schema_routes
from agent_system.stream.emitter import StreamEmitter, wrap_streaming_generator

# 项目根目录（agent_system/ 的上一级），static/ 与 data/ 都位于此
BASE_DIR = Path(__file__).resolve().parent.parent

# 1) 先读配置并初始化日志：后续所有模块打日志时格式已经就位
settings = get_settings()
configure_logging(settings)

# 记录本次启动生效的配置，便于事后对账（不打印任何密钥类字段）
config_loaded(
    service=settings.service_name,
    log_level=settings.log_level_name,
    log_format=settings.log_format,
    cors_enabled=bool(settings.cors_allow_origins),
    cors_origins_count=len(settings.cors_allow_origins),
    rate_limit_enabled=settings.rate_limit_enabled,
    rate_limit_window_seconds=settings.rate_limit_window_seconds,
    rate_limit_max_requests=settings.rate_limit_max_requests,
    rate_limit_exclude_paths=list(settings.rate_limit_exclude_paths),
    max_body_bytes=settings.max_body_bytes,
    context_max_history=settings.context_max_history,
    context_max_total_tokens=settings.context_max_total_tokens,
    context_reserve_completion_tokens=settings.context_reserve_completion_tokens,
    context_min_keep_recent=settings.context_min_keep_recent,
    context_max_user_chars=settings.context_max_user_chars,
    context_stable_prefix=settings.context_stable_prefix,
    loop_max_steps=settings.loop_max_steps,
    loop_max_tool_calls=settings.loop_max_tool_calls,
    loop_timeout_seconds=settings.loop_timeout_seconds,
    loop_enable_interrupt_check=settings.loop_enable_interrupt_check,
    confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
    confirmation_enabled=settings.confirmation_enabled,
    confirmation_max_pending_per_session=settings.confirmation_max_pending_per_session,
    retry_enabled=settings.retry_enabled,
    retry_max_attempts=settings.retry_max_attempts,
    retry_initial_backoff_ms=settings.retry_initial_backoff_ms,
    retry_max_backoff_ms=settings.retry_max_backoff_ms,
    retry_backoff_multiplier=settings.retry_backoff_multiplier,
    retry_jitter_ratio=settings.retry_jitter_ratio,
    retry_llm=settings.retry_llm,
    retry_tools=settings.retry_tools,
    retry_fallback_enabled=settings.retry_fallback_enabled,
    router_strategy=settings.router_strategy,
    router_confidence_threshold=settings.router_confidence_threshold,
    router_default_profile=settings.router_default_profile,
    router_llm_enabled=settings.router_llm_enabled,
    router_llm_timeout_seconds=settings.router_llm_timeout_seconds,
)

# 初始化观测层：未配置密钥/鉴权失败都会安全降级为 no-op，不阻塞启动
init_langfuse(settings)

# 默认兜底钩子：未开启 Retry 时只记录 error_raised；
# 开启 Retry 时换成观察实现（额外记录 retry_exhausted_observed，不改变异常流向）。
# 降级回复只在 reply / reply_stream 内部处理，钩子不负责改写响应。
if get_retry_policy().enabled:
    register_hook(RetryObservingHook())
else:
    register_hook(NoOpFallbackHook())

# 把内置工具装入 Registry：必须在 FastAPI 实例创建前完成，
# 这样首个请求到达时 LLMClient.TOOLS 已经有 schema 可用（register_all 幂等）。
register_builtin_tools()
# Profile 依赖 Tool Registry 的 write 标签，因此必须在工具注册之后
register_builtin_agent_profiles()

app = FastAPI(title="电商AI客服")


def _disable_native_telemetry(application) -> None:
    """关闭 FastAPI 0.162+ 内置的 OpenTelemetry telemetry。

    参数:
        application: FastAPI 实例。

    返回:
        无；若当前版本没有 `_telemetry` 配置则静默跳过。

    说明:
        FastAPI 0.162 起默认开启内置 telemetry，会借助全局 TracerProvider 额外生成
        “POST /chat” 之类的 trace，与 langfuse_mapping 重复，导致一次请求出现两条
        trace。FastAPI 构造后没有公开关闭入口，只能调整这个配置字典。
    """
    telemetry = getattr(application, "_telemetry", None)
    if isinstance(telemetry, dict):
        for key in ("tracing", "metrics", "logs", "operation_spans"):
            telemetry[key] = False


_disable_native_telemetry(app)

# 有 Key 用真实 LLM（默认 DeepSeek），否则用本地 Mock，保证服务随时可启动
bot = (
    LLMClient()
    if (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))
    else MockLLM()
)
store = create_store()

# 会话状态缓存：Redis 未配置时内部为 no-op；已配置但不可达时，
# 若允许回退则 create_store() 已回退 SQLite，而会话状态没有 SQLite 等价实现，
# 因此直接禁用缓存即可，写失败会在请求期以事件形式可见，不影响启动。
try:
    state_cache = get_session_state_cache()
except RedisUnavailable:  # pragma: no cover - 当前实现惰性连接，不会在此抛出
    state_cache = None

# 启动期显式选择 pending 后端：让“Redis 已配置但不可达”在启动阶段暴露，
# 而不是拖到首个 confirm 请求才失败。与 create_store() 的显式失败口径一致。
# PENDING_STORE_BACKEND=memory 时不会触碰 Redis，因此不会在这里抛错。
try:
    _pending_store = get_pending_store()
    # 启动期是运维定位故障的关键时刻，明确记录后端选择便于排障（不记连接串）
    log_event(
        "pending_store_bootstrapped",
        backend=type(_pending_store).__name__,
    )
except RedisUnavailable:
    # 显式失败：pending 是写操作的两阶段状态，分叉会导致重复执行，宁可启动失败
    raise

# 归一化消息缓存：构造时不连接 Redis；未配置时所有操作 no-op。
nmsg_cache = get_nmsg_cache()

ESCALATION_TIP = "\n\n（检测到您可能需要人工协助，可联系在线人工客服进一步处理）"


class ChatIn(BaseModel):
    session_id: str
    message: str


class ChatOut(BaseModel):
    reply: str
    # 可选扩展：旧客户端只读 reply，新增字段不影响解析
    route: dict | None = None
    # 归一化消息缓存结果；默认关闭时为 None（响应中不出现该字段）
    cache: dict | None = None


def _strategy_of(decision: RouteDecision | None, reason: str) -> str:
    """把 Router 决策与选择原因转换为对外 strategy 字符串。"""
    if reason == "pending_override":
        return "pending_override"
    if decision is not None:
        return decision.strategy.value
    return "fallback"


def _route_payload(
    profile,
    decision: RouteDecision | None,
    reason: str,
) -> dict:
    """构造 /chat 的 route 字段。

    参数:
        profile: 最终使用的 Profile。
        decision: Router 决策；pending 粘滞时为 None。
        reason: 选择原因（pending_override / router / profile_missing / ...）。

    返回:
        不含用户消息原文与 LLM 原始响应的安全摘要。
    """
    return {
        "agent": profile.name,
        "display_name": profile.display_name,
        "strategy": _strategy_of(decision, reason),
        # pending 粘滞是确定性规则，置信度给 1.0；Router 异常回退给 0
        "confidence": (decision.confidence if decision is not None else (1.0 if reason == "pending_override" else 0.0)),
        "reason": reason,
    }


def _record_route_state(session_id, profile, decision, reason) -> None:
    """把本次路由结果写入会话状态缓存（旁路，失败绝不影响请求）。

    参数:
        session_id: 会话 ID。
        profile: 选中的 AgentProfile。
        decision: Router 决策；pending 粘滞时为 None。
        reason: 选择原因。

    说明:
        - 与会话级 `route` 字段共用 `_route_payload` 的计算口径，避免两处漂移；
        - 缓存层本身不抛 RedisUnavailable；这里再兜一层异常，
          防止未来实现变化把缓存故障传导到 /chat；
        - 不写入用户消息原文，只有路由元数据。
    """
    if state_cache is None or profile is None:
        return
    try:
        route = _route_payload(profile, decision, reason)
        state_cache.set_route(
            session_id,
            agent=route["agent"],
            display_name=route["display_name"],
            strategy=route["strategy"],
            confidence=route["confidence"],
            reason=route["reason"],
        )
    except Exception:  # noqa: BLE001 - 旁路能力，任何异常都不能影响响应
        pass


def _nmsg_cache_enabled_for(profile) -> bool:
    """判断该 Profile 是否启用归一化消息缓存。

    条件（全部满足才启用）:
        1. 全局开关 NMSG_CACHE_ENABLED=true；
        2. profile.name 在 NMSG_CACHE_ENABLED_PROFILES 中；
        3. Redis 已配置（未配置时缓存层全部 no-op）。

    说明:
        after_sales 默认不在白名单：它涉及两阶段确认与订单状态变化，
        缓存可能让用户看到旧的“待确认”状态，或跳过确认流程。
    """
    if not settings.nmsg_cache_enabled or profile is None:
        return False
    if profile.name not in settings.nmsg_cache_enabled_profiles:
        return False
    return nmsg_cache is not None and nmsg_cache.is_enabled()


def _profile_tools_for_hash(profile) -> list[dict]:
    """取该 Profile 的工具 schema，用于计算稳定前缀哈希。

    参数:
        profile: 当前 AgentProfile。

    返回:
        与 `_build_messages` 下发一致的 schema 列表。

    说明:
        LLMClient 有 `schemas_for`，直接复用；
        Mock / 假 bot 没有该方法时，按 profile.tool_names 做同样的过滤，
        保证哈希按 profile 隔离（否则 pre_sales 与 order 会互相命中）。
    """
    if hasattr(bot, "schemas_for"):
        try:
            return bot.schemas_for(profile)
        except Exception:  # noqa: BLE001 - 哈希计算失败时退回注册表
            pass

    registry = get_registry()
    available = []
    for name in profile.tool_names:
        spec = registry.get(name)
        if spec is not None and spec.enabled:
            available.append(name)
    if available:
        return registry.schemas(names=available)
    return registry.schemas()


def _resolve_stable_prefix_hash(profile) -> str:
    """计算与 ContextAssembler 一致的稳定前缀哈希。

    参数:
        profile: 当前 AgentProfile。

    返回:
        sha256(系统提示词 + "\\n" + 排序后的工具 schema)[:16]。

    说明:
        - 必须与 Assembler 的口径逐字一致：Profile prompt 或工具 schema 一变，
          缓存 key 就必须变化，否则会命中旧能力的回复；
        - 不直接复用 `bot._last_assembled`：它可能对应上一次请求的另一个 Profile，
          而该对象没有记录归属，复用会造成跨 Profile 串缓存；
        - 不调用 `_build_messages`：那会触发 Assembler 并消耗上下文预算，
          而查缓存发生在 LLM 调用之前。
    """
    system_prompt = compose_system_prompt(profile)
    tools_schemas = _profile_tools_for_hash(profile)

    tools_str = ""
    policy = get_context_assembler().policy
    if tools_schemas and policy.stable_prefix_mode:
        ordered = sorted(
            tools_schemas,
            key=lambda schema: str(schema.get("function", {}).get("name", "")),
        )
        tools_str = json.dumps(ordered, sort_keys=True, ensure_ascii=False)

    raw = system_prompt + "\n" + tools_str
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _nmsg_ttl_for(profile) -> int:
    """返回该 Profile 的缓存 TTL（order 更短，因为订单状态变化快）。"""
    if profile is not None and profile.name == "order":
        return settings.nmsg_order_ttl_seconds
    return settings.nmsg_ttl_seconds


def _nmsg_cache_lookup(profile, message: str, stable_prefix_hash: str) -> str | None:
    """查归一化消息缓存；不启用或未命中时返回 None。

    参数:
        profile: 当前 Profile。
        message: 用户原始消息。
        stable_prefix_hash: 稳定前缀哈希。

    返回:
        命中的回复文本；否则 None。

    说明:
        全局关闭时直接返回，不记任何事件（保证默认配置零行为变化）；
        其余未命中路径记录 cache_miss，便于阶段六统计命中率。
    """
    if not settings.nmsg_cache_enabled:
        return None
    if not _nmsg_cache_enabled_for(profile):
        log_event(
            "cache_miss",
            profile=profile.name if profile is not None else "-",
            layer="nmsg",
            reason="not_enabled",
        )
        return None

    normalized = normalize_message(message)
    if not is_cacheable(normalized):
        # 短消息/纯数字不查缓存：它们本来就不允许写入，避免无意义的 Redis IO
        return None

    cached = nmsg_cache.get(stable_prefix_hash, message)
    if cached is not None:
        log_event("cache_hit", profile=profile.name, layer="nmsg")
        return cached

    log_event("cache_miss", profile=profile.name, layer="nmsg", reason="not_found")
    return None


def _nmsg_cache_maybe_store(
    profile, message: str, stable_prefix_hash: str, reply: str, used_tools
) -> None:
    """满足条件时写回缓存；任何异常都不影响请求。

    写回条件（全部满足）:
        1. 全局开启且 Profile 在白名单；
        2. 未调用带 write 标签的工具（apply_refund / cancel_order）；
        3. 未调用带 internal 标签的工具（confirm_action / reject_action）；
        4. reply 非空；
        5. 归一化消息通过长度/纯数字检查。

    说明:
        - 写操作与确认流程的结果不应被复用，否则用户可能看到“旧的成功”，
          实际写操作并未执行；
        - 未触发任何工具（used_tools 为空）视为可缓存（例如纯 FAQ 回复）。
    """
    if not settings.nmsg_cache_enabled:
        return
    profile_name = profile.name if profile is not None else "-"
    if profile is None or profile.name not in settings.nmsg_cache_enabled_profiles:
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="profile_not_in_whitelist",
        )
        return
    if nmsg_cache is None or not nmsg_cache.is_enabled():
        # Redis 未配置：缓存整体 no-op，不产生降级噪音
        return

    registry = get_registry()
    specs = [registry.get(name) for name in (used_tools or [])]
    if any(spec is not None and "write" in spec.tags for spec in specs):
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="write_tool_used",
        )
        return
    if any(spec is not None and "internal" in spec.tags for spec in specs):
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="internal_tool_used",
        )
        return
    if not reply:
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="reply_empty",
        )
        return
    if not is_cacheable(normalize_message(message)):
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="not_cacheable",
        )
        return

    ttl = _nmsg_ttl_for(profile)
    if nmsg_cache.put(stable_prefix_hash, message, reply, ttl_seconds=ttl):
        log_event(
            "cache_write",
            profile=profile_name,
            layer="nmsg",
            ttl_seconds=ttl,
        )
    else:
        # NmsgCache 已记录 cache_put_failed；这里补充“本次写回未成功”的决策结果
        log_event(
            "cache_write_skipped",
            profile=profile_name,
            layer="nmsg",
            reason="redis_error",
        )


def _chunk(text: str, size: int):
    """把缓存回复按字符分块，模拟流式输出。

    参数:
        text: 完整回复。
        size: 每块字符数（默认 20，启发式值，不是协议要求）。

    说明:
        命中时如果一次性下发大段文本，前端的打字机效果会突然消失；
        分块能让“缓存命中”与“真实流式”在视觉上保持一致。
    """
    step = max(1, int(size))
    for index in range(0, len(text), step):
        yield text[index : index + step]


@app.post("/chat", response_model=ChatOut, response_model_exclude_none=True)
def chat(body: ChatIn):
    history = store.get(body.session_id)
    # 会话级选择：pending 粘滞 -> Router -> 默认 Profile
    profile, decision, reason = select_profile(body.session_id, body.message)
    # 写入会话状态缓存：仅旁路记录最近路由，不改变响应
    _record_route_state(body.session_id, profile, decision, reason)

    # 缓存关闭时完全不计算稳定前缀、不查 Redis，保持既有行为零变化。
    cache_payload = None
    if settings.nmsg_cache_enabled:
        # 查缓存必须在 profile 确定之后：key 依赖 profile 的稳定前缀哈希。
        stable_prefix_hash = _resolve_stable_prefix_hash(profile)
        cached_reply = _nmsg_cache_lookup(profile, body.message, stable_prefix_hash)

        if cached_reply is not None:
            # 命中：跳过 bot.reply 与全部 LLM 调用
            reply = cached_reply
            cache_payload = {"hit": True, "layer": "nmsg"}
        else:
            reply = bot.reply(body.message, history=history, profile=profile)
            cache_payload = {"hit": False, "layer": "nmsg"}
            # 写回放在得到回复且未抛异常之后：失败请求不产生缓存
            _nmsg_cache_maybe_store(
                profile,
                body.message,
                stable_prefix_hash,
                reply,
                used_tools=getattr(bot, "_last_used_tools", []),
            )
    else:
        reply = bot.reply(body.message, history=history, profile=profile)

    # 转人工判断基于用户消息，与回复是否来自缓存无关，保持既有行为
    if detect_escalation(body.message):
        reply += ESCALATION_TIP
    store.append(body.session_id, "user", body.message)
    store.append(body.session_id, "assistant", reply)
    return ChatOut(
        reply=reply,
        route=_route_payload(profile, decision, reason),
        cache=cache_payload,
    )


def _original_stream_logic(body: ChatIn, profile):
    """原 `/chat/stream` 的生成逻辑，逐段保留，不改变既有 NDJSON 行结构。

    参数:
        body: 请求体（session_id / message）。
        profile: 本次选中的子 Agent，用于限定工具子集与 prompt。

    返回:
        同步生成器，依次产出 `{"delta": ...}` 与 `{"done": true}` 行。

    说明:
        协议扩展（start / route / error / done 元数据）由外层
        `wrap_streaming_generator` 与本函数的 route 前导行共同注入；
        本函数只负责业务内容，确保旧前端解析逻辑不受影响。
    """
    history = store.get(body.session_id)
    full = ""
    escalated = detect_escalation(body.message)
    for chunk in bot.reply_stream(body.message, history=history, profile=profile):
        full += chunk
        yield json.dumps({"delta": chunk}, ensure_ascii=False) + "\n"
    if escalated:
        full += ESCALATION_TIP
        yield json.dumps({"delta": ESCALATION_TIP}, ensure_ascii=False) + "\n"
    store.append(body.session_id, "user", body.message)
    store.append(body.session_id, "assistant", full)
    yield json.dumps({"done": True}) + "\n"


@app.post("/chat/stream")
def chat_stream(body: ChatIn):
    """流式返回 NDJSON：start -> route -> [cache] -> delta... -> {"done": true} -> 扩展 done。

    说明:
        request_id 与 trace_id 在端点入口（仍在请求上下文内）显式读取并传给
        emitter；route 事件在 start 之后、任何 delta 之前发出，便于前端在
        首个 token 前更新 UI，且不受后续业务失败影响。
        cache 事件仅在 NMSG_CACHE_ENABLED=true 时出现，位于 route 之后、
        首个 delta 之前；默认关闭时序列与阶段五步骤 3.5 完全一致。
    """
    emitter = StreamEmitter(
        request_id=get_request_id(),
        session_id=body.session_id,
        trace_id=current_trace_id(),
        trace_id_provider=stream_trace_id_provider,
    )

    def _stream_with_route():
        """在原始业务流前插入 route 事件。"""
        profile, decision, reason = select_profile(body.session_id, body.message)
        # 与 /chat 一致：旁路记录最近路由到会话状态缓存，不改变 NDJSON 结构
        _record_route_state(body.session_id, profile, decision, reason)
        route_event = emitter.emit_route(
            agent=profile.name,
            confidence=(
                decision.confidence
                if decision is not None
                else (1.0 if reason == "pending_override" else 0.0)
            ),
            reason=reason,
            display_name=profile.display_name,
            strategy=_strategy_of(decision, reason),
        )
        yield json.dumps(route_event, ensure_ascii=False) + "\n"

        # 命中缓存时跳过 bot.reply_stream 与全部 LLM 调用；
        # 未命中/关闭时保持原有 delta 序列不变。
        stable_prefix_hash = ""
        cached_reply = None
        if settings.nmsg_cache_enabled:
            stable_prefix_hash = _resolve_stable_prefix_hash(profile)
            cached_reply = _nmsg_cache_lookup(profile, body.message, stable_prefix_hash)

        if cached_reply is not None:
            reply = cached_reply
            if detect_escalation(body.message):
                reply += ESCALATION_TIP
            yield json.dumps(
                emitter.emit_cache(hit=True, layer="nmsg"), ensure_ascii=False
            ) + "\n"
            # 按字符分块，保持与真实流式一致的打字机体验
            for chunk in _chunk(reply, settings.nmsg_stream_chunk_size):
                yield json.dumps({"delta": chunk}, ensure_ascii=False) + "\n"
            store.append(body.session_id, "user", body.message)
            store.append(body.session_id, "assistant", reply)
            yield json.dumps({"done": True}) + "\n"
            return

        if settings.nmsg_cache_enabled:
            # 仅当缓存功能开启时才新增该事件，默认关闭保持旧 NDJSON 序列
            yield json.dumps(
                emitter.emit_cache(hit=False, layer="nmsg"), ensure_ascii=False
            ) + "\n"

        collected: list[str] = []
        for line in _original_stream_logic(body, profile):
            yield line
            # 收集 delta 文本用于成功后写回；解析失败的行（理论上不存在）直接忽略
            try:
                payload = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            delta = payload.get("delta")
            if isinstance(delta, str):
                collected.append(delta)

        full_reply = "".join(collected)
        # 写回前去掉转人工提示：命中路径会重新追加，避免重复写入提示文案
        if detect_escalation(body.message) and full_reply.endswith(ESCALATION_TIP):
            full_reply = full_reply[: -len(ESCALATION_TIP)]
        _nmsg_cache_maybe_store(
            profile,
            body.message,
            stable_prefix_hash,
            full_reply,
            used_tools=getattr(bot, "_last_used_tools", []),
        )

    return StreamingResponse(
        wrap_streaming_generator(_stream_with_route(), emitter),
        media_type="application/x-ndjson",
    )


@app.post("/webhook/{platform}")
async def webhook(platform: str, request: Request):
    """平台消息回调入口：验签 -> 归一化 -> 交给 Agent -> 返回回复。

    真实接入时，各平台会主动推送消息到这里，需按其签名算法验证请求来源。
    """
    payload = await request.json()
    # TODO: 按平台验签，例如 verify_signature(platform, request.headers, payload)
    msg = normalize(platform, payload)
    if not msg["message"]:
        return {"error": "empty message"}

    history = store.get(msg["session_id"])
    profile, decision, reason = select_profile(msg["session_id"], msg["message"])
    reply = bot.reply(msg["message"], history=history, profile=profile)
    if detect_escalation(msg["message"]):
        reply += ESCALATION_TIP
    store.append(msg["session_id"], "user", msg["message"])
    store.append(msg["session_id"], "assistant", reply)

    # 真实平台通常需要主动推送回复（而非同步返回），这里先返回统一结构
    return {"session_id": msg["session_id"], "reply": reply}


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


# 5) 注册异常处理器、健康检查与协议自查路由
register_exception_handlers(app)
register_health_routes(app)
register_stream_schema_routes(app)

# 6) 注册中间件。
# add_middleware 是「往洋葱里再套一层」：越晚注册越靠外层。
# 因此下面这四行的书写顺序是实际执行顺序的反向，目标语义顺序（外 -> 内）为：
#   request_id -> rate_limit -> body_limit -> cors -> 业务路由
# 这样 request_id 在最外层，被限流(429)或体积超限(400)提前拒绝的请求也能带上响应头。
configure_cors(app, settings)
app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestContextMiddleware)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

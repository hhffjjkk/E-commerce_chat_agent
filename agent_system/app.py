# app.py
# FastAPI 服务入口：多轮对话（含流式）、平台回调、健康检查与协议自查。
# 启动：uvicorn agent_system.app:app --host 127.0.0.1 --port 8000
#
# 本文件同时承担“装配层”职责：日志/观测初始化、错误处理器、中间件与健康路由
# 都在这里完成；不再有外部包装层或运行期 monkey-patch。

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_system.agent import LLMClient, MockLLM, detect_escalation
from agent_system.channels import normalize
from agent_system.config import get_settings
from agent_system.errors.fallback import NoOpFallbackHook, register_hook
from agent_system.errors.handlers import register_exception_handlers
from agent_system.harness.builtin_tools import register_all as register_builtin_tools
from agent_system.memory import create_store
from agent_system.obs.events import config_loaded
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
)

# 初始化观测层：未配置密钥/鉴权失败都会安全降级为 no-op，不阻塞启动
init_langfuse(settings)

# 默认兜底钩子：只记录 error_raised，不重试、不降级（阶段三替换实现）
register_hook(NoOpFallbackHook())

# 把内置工具装入 Registry：必须在 FastAPI 实例创建前完成，
# 这样首个请求到达时 LLMClient.TOOLS 已经有 schema 可用（register_all 幂等）。
register_builtin_tools()

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

ESCALATION_TIP = "\n\n（检测到您可能需要人工协助，可联系在线人工客服进一步处理）"


class ChatIn(BaseModel):
    session_id: str
    message: str


class ChatOut(BaseModel):
    reply: str


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn):
    history = store.get(body.session_id)
    reply = bot.reply(body.message, history=history)
    if detect_escalation(body.message):
        reply += ESCALATION_TIP
    store.append(body.session_id, "user", body.message)
    store.append(body.session_id, "assistant", reply)
    return ChatOut(reply=reply)


def _original_stream_logic(body: ChatIn):
    """原 `/chat/stream` 的生成逻辑，逐段保留，不改变既有 NDJSON 行结构。

    参数:
        body: 请求体（session_id / message）。

    返回:
        同步生成器，依次产出 `{"delta": ...}` 与 `{"done": true}` 行。

    说明:
        协议扩展（start / error / done 元数据）由外层 `wrap_streaming_generator`
        统一注入；本函数只负责业务内容，确保旧前端解析逻辑不受影响。
    """
    history = store.get(body.session_id)
    full = ""
    escalated = detect_escalation(body.message)
    for chunk in bot.reply_stream(body.message, history=history):
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
    """流式返回 NDJSON：start -> delta... -> {"done": true} -> 扩展 done。

    说明:
        request_id 与 trace_id 在端点入口（仍在请求上下文内）显式读取并传给
        emitter，因为流式迭代阶段 contextvars 可能已退出当前请求上下文；
        provider 作为兜底，仅在显式 trace_id 为空时才会被调用一次。
    """
    emitter = StreamEmitter(
        request_id=get_request_id(),
        session_id=body.session_id,
        trace_id=current_trace_id(),
        trace_id_provider=stream_trace_id_provider,
    )
    return StreamingResponse(
        wrap_streaming_generator(_original_stream_logic(body), emitter),
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
    reply = bot.reply(msg["message"], history=history)
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

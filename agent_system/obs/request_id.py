# agent_system/obs/request_id.py
# 纯 ASGI 请求上下文中间件：生成/复用 request_id，写入响应头与登录上下文。
#
# 为什么不用 BaseHTTPMiddleware：
#   它会额外包一层任务组，导致 StreamingResponse 的响应头改写、以及 contextvars
#   在流式场景的传播都更容易出偏差。纯 ASGI 可以直接改写 http.response.start /
#   http.response.body 消息，行为更可控。

import json
import re
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders

# 对外约定的请求 ID 头；入站复用、出站回写都用它
REQUEST_ID_HEADER = "X-Request-ID"

# 合法请求 ID：8~64 位，只允许字母数字与 . _ : -（够宽松又排除空白/控制字符）
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")

# 最多缓存多少请求体用于解析 session_id，避免大请求体被整个复制进内存
_MAX_BODY_CAPTURE = 64 * 1024

# 请求上下文：默认值 "-"，保证在请求之外调用也不会抛异常
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
_session_id_var: ContextVar[str] = ContextVar("session_id", default="")


def get_request_id() -> str:
    """取当前请求的 request_id；不在请求上下文中时返回 "-"。"""
    return _request_id_var.get() or "-"


def get_session_id() -> str | None:
    """取当前请求的 session_id；未解析到或不在上下文中时返回 None。"""
    return _session_id_var.get() or None


def set_session_id(session_id: str):
    """显式设置当前上下文的 session_id（供 CLI/demo 等非 HTTP 入口使用）。

    参数:
        session_id: 会话 ID；空值会写成空串（get_session_id 仍返回 None）。

    返回:
        contextvars.Token，可用 reset_session_id 恢复。

    说明:
        HTTP 入口由 RequestContextMiddleware 自动从请求体解析并设置，无需调用本函数。
    """
    return _session_id_var.set((session_id or "")[:128])


def reset_session_id(token) -> None:
    """恢复 set_session_id 之前的上下文。"""
    _session_id_var.reset(token)


def new_request_id() -> str:
    """生成新的请求 ID：16 位十六进制，足够短便于展示，碰撞概率可忽略。"""
    return uuid.uuid4().hex[:16]


def _pick_request_id(incoming: str | None) -> str:
    """优先复用入站 X-Request-ID（格式合法时），否则生成新 ID。

    复用是为了让上游网关/前端生成的 ID 能贯穿整条链路；
    校验格式是为了防止把超长字符串或控制字符写进响应头。
    """
    if incoming:
        candidate = incoming.strip()
        if _REQUEST_ID_RE.fullmatch(candidate):
            return candidate
    return new_request_id()


def _extract_session_id(body: bytes) -> str | None:
    """从请求体 JSON 中尽力提取 session_id，供后续日志自动补齐。

    解析失败、非对象、字段缺失或非字符串都返回 None —— 这只是日志增强，
    绝不能因为解析失败影响请求本身。
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    if isinstance(session_id, str):
        session_id = session_id.strip()
        if session_id:
            # 截断到 128 字符，避免异常长值污染日志与响应
            return session_id[:128]
    return None


def _chunk_has_done_event(chunk: bytes) -> bool:
    """判断响应块的最后一行是否已是流式协议的扩展 done 事件。

    参数:
        chunk: 单个 http.response.body 消息携带的字节内容。

    返回:
        若最后一行是可解析 JSON 且 event == "done" 则返回 True，否则 False。

    说明:
        步骤 4 起由 StreamEmitter 在流末尾发射带 status/latency 的 done 事件；
        此处用于避免再追加步骤 1 的简版 done 行，保证协议中只有一个 done 事件。
    """
    for line in reversed(chunk.splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("event") == "done"
    return False


class RequestContextMiddleware:
    """为每个 HTTP 请求建立 request_id 上下文，并输出 start/end/error 事件。"""

    def __init__(self, app):
        """保存下游 ASGI 应用；本中间件不读取任何配置。"""
        self.app = app

    async def __call__(self, scope, receive, send):
        """ASGI 入口：非 HTTP 请求直接放行，HTTP 请求包一层上下文与日志。"""
        if scope["type"] != "http":
            # WebSocket / lifespan 不做处理，避免干扰应用的启动与关闭流程
            await self.app(scope, receive, send)
            return

        # 延迟导入，避免 events <-> request_id 的模块循环
        from agent_system.obs.events import request_end, request_error, request_start

        headers = Headers(scope=scope)
        request_id = _pick_request_id(headers.get("x-request-id"))
        method = scope.get("method", "")
        path = scope.get("path", "")
        start = time.perf_counter()

        # 写入 contextvars：后续任意深度的代码都能通过 get_request_id() 取到
        request_token = _request_id_var.set(request_id)
        session_token = _session_id_var.set("")

        # 同时写入 scope["state"]：异常处理器在 contextvars 重置后仍能读取
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["start_perf"] = start

        request_start(request_id, method, path)

        # 请求体捕获状态（仅用于解析 session_id）
        body_chunks: list[bytes] = []
        body_size = 0
        # 响应状态跟踪：用于判断异常时响应是否已经开始发送
        response_started = False
        response_status = 500
        stream_done_appended = False
        saw_stream_done_event = False
        last_body_ended_with_newline = True

        async def receive_wrapper():
            """拦截请求体，在最后一块到达时解析出 session_id 写入上下文。"""
            nonlocal body_size
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                if body_size < _MAX_BODY_CAPTURE and chunk:
                    remain = _MAX_BODY_CAPTURE - body_size
                    body_chunks.append(chunk[:remain])
                    body_size += len(chunk[:remain])
                if not message.get("more_body", False):
                    # 最后一块：此时 body 才是完整的，再解析才有意义
                    session_id = _extract_session_id(b"".join(body_chunks))
                    if session_id:
                        _session_id_var.set(session_id)
                        state["session_id"] = session_id
            return message

        async def send_wrapper(message):
            """拦截响应消息：注入 X-Request-ID，并为 /chat/stream 追加 done 事件。"""
            nonlocal response_started, response_status, stream_done_appended
            nonlocal last_body_ended_with_newline, saw_stream_done_event

            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                # 响应头一旦发出就无法再改，所以必须在这里注入
                mutable = MutableHeaders(scope=message)
                mutable[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body":
                raw = message.get("body", b"") or b""
                more_body = message.get("more_body", False)
                has_done_event = _chunk_has_done_event(raw)

                # 只在流正常结束（200 + 最后一块）时追加一行 done 事件。
                # 既有 {"delta": ...} / {"done": true} 保持原样，不新增其他字段。
                # 步骤 4 起若 StreamEmitter 已发射扩展 done，则跳过追加避免重复。
                if (
                    path == "/chat/stream"
                    and response_status == 200
                    and not more_body
                    and not stream_done_appended
                    and not saw_stream_done_event
                    and not has_done_event
                ):
                    body = raw
                    if body:
                        # 保证前一行完整结束，否则拼出来的 NDJSON 无法逐行解析
                        if not body.endswith(b"\n"):
                            body += b"\n"
                    elif not last_body_ended_with_newline:
                        body = b"\n"
                    body += (
                        json.dumps(
                            {"event": "done", "request_id": request_id},
                            ensure_ascii=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    message = {**message, "body": body}
                    stream_done_appended = True

                # 记录本请求是否已经出现扩展 done；空结尾块继续沿用该状态
                if has_done_event:
                    saw_stream_done_event = True

                if raw:
                    last_body_ended_with_newline = raw.endswith(b"\n")

            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as exc:
            # 只记录不吞掉：异常继续上抛，由 ServerErrorMiddleware 的处理器兜底
            request_error(
                request_id=request_id,
                method=method,
                path=path,
                error_type=type(exc).__name__,
                error_message=str(exc),
                session_id=get_session_id(),
            )
            raise
        finally:
            # 只在响应已开始时记 request_end；未开始说明异常发生在响应前，
            # 由全局异常处理器补充记录，避免同一请求出现两条 request_end。
            if response_started:
                request_end(
                    request_id=request_id,
                    method=method,
                    path=path,
                    status=response_status,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    session_id=get_session_id(),
                )
            # 复位 contextvars，避免串到同一 worker 的下一个请求
            _session_id_var.reset(session_token)
            _request_id_var.reset(request_token)

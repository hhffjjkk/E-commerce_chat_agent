# agent_system/obs/events.py
# 结构化事件构造函数：统一补齐 request_id / session_id，且日志失败不影响业务。
# 约定：每个函数只负责组装字段并调用 log_event，不在这里做任何业务判断。

from agent_system.obs.logging_config import get_logger
from agent_system.obs.request_id import get_request_id, get_session_id


def _truncate(value, limit: int = 200) -> str:
    """把长文本截断到 limit 字符，避免异常消息把一行日志撑爆。"""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def log_event(event: str, **fields) -> None:
    """输出一条结构化事件；任何日志异常都被吞掉，避免影响请求。

    参数：
        event: 事件名，会作为 JSON 里的 event 字段，同时用作日志消息文本。
        **fields: 额外字段；若显式传入 request_id 则优先使用，否则从上下文取。
    """
    # 显式传入的 request_id 优先（用于异常处理器在上下文已重置之后补日志）
    request_id = fields.pop("request_id", None) or get_request_id()
    payload = {"request_id": request_id or "-"}

    # 调用方没写 session_id 时，自动从请求上下文补齐
    if "session_id" not in fields:
        session_id = get_session_id()
        if session_id:
            payload["session_id"] = session_id
    payload.update(fields)

    try:
        get_logger().info(event, extra={"event": event, **payload})
    except Exception:
        # 日志是旁路能力，不能因为序列化/权限问题打断请求
        pass

    # Langfuse 映射是日志的“额外消费端”：启用时转发，未启用时直接返回。
    # 延迟导入避免 events <-> langfuse_mapping 的模块循环；任何异常都吞掉。
    try:
        from agent_system.obs import langfuse_mapping

        langfuse_mapping.handle_event(event, payload)
    except Exception:
        pass


def request_start(request_id: str, method: str, path: str) -> None:
    """请求进入中间件时记录：谁、用什么方法、打了哪个路径。"""
    log_event(
        "request_start",
        request_id=request_id,
        method=method,
        path=path,
    )


def request_end(
    request_id: str,
    method: str,
    path: str,
    status: int,
    latency_ms: float,
    session_id: str | None = None,
) -> None:
    """请求返回时记录：状态码与端到端耗时，用于统计 P50/P95 与错误率。"""
    fields = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "status": status,
        "latency_ms": round(latency_ms, 2),
    }
    if session_id:
        fields["session_id"] = session_id
    log_event("request_end", **fields)


def request_error(
    request_id: str,
    method: str,
    path: str,
    error_type: str,
    error_message: str,
    session_id: str | None = None,
) -> None:
    """未被捕获的异常发生时记录：异常类型 + 截断后的消息（不含堆栈）。"""
    fields = {
        "request_id": request_id,
        "method": method,
        "path": path,
        "error_type": error_type,
        "error_message": _truncate(error_message),
    }
    if session_id:
        fields["session_id"] = session_id
    log_event("request_error", **fields)


def error_raised(
    code: str,
    retryable: bool,
    cause_type: str | None = None,
    *,
    request_id: str | None = None,
    path: str | None = None,
    tool: str | None = None,
    session_id: str | None = None,
) -> None:
    """领域异常统一事件；cause_type 只记异常类型，不记异常文本/堆栈。

    与 request_error 的区别：这是「已分类」的错误（有错误码、有是否可重试），
    request_error 是「没被分类」的兜底异常。
    """
    fields = {"code": code, "retryable": retryable}
    if request_id:
        fields["request_id"] = request_id
    if cause_type:
        fields["cause_type"] = cause_type
    if path:
        fields["path"] = path
    if tool:
        fields["tool"] = tool
    if session_id:
        fields["session_id"] = session_id
    log_event("error_raised", **fields)


def config_loaded(**fields) -> None:
    """启动时记录已生效配置（不含密钥等敏感值），便于事后对账线上参数。"""
    log_event("config_loaded", **fields)


def health_check(kind: str, status: str, checks: dict | None = None) -> None:
    """记录健康检查结果；kind 区分 liveness / readiness，checks 携带分项详情。"""
    fields = {"kind": kind, "status": status}
    if checks is not None:
        fields["checks"] = checks
    log_event("health_check", **fields)


def rate_limited(ip: str, path: str, limit: int, window: float) -> None:
    """记录一次被限流拒绝的请求，便于评估阈值是否过紧。"""
    log_event("rate_limited", ip=ip, path=path, limit=limit, window=window)


def llm_call(
    phase: str,
    ok: bool | None,
    model: str | None,
    stream: bool,
    latency_ms: float | None = None,
    error_type: str | None = None,
) -> None:
    """记录一次 LLM 调用。调用前后各记一次：

    phase="start" 时 ok 传 None（还没有结果），不带 latency_ms；
    phase="end" 时带 ok / latency_ms，失败时附 error_type。
    注意：stream=True 时 latency_ms 只覆盖「拿到流对象」的耗时，不含逐块消费。
    """
    fields = {
        "phase": phase,
        "model": model,
        "stream": stream,
    }
    if ok is not None:
        fields["ok"] = ok
    if latency_ms is not None:
        fields["latency_ms"] = round(latency_ms, 2)
    if error_type:
        fields["error_type"] = error_type
    log_event("llm_call", **fields)


def tool_call(
    tool: str,
    args_keys: list[str],
    ok: bool,
    latency_ms: float,
    error_type: str | None = None,
) -> None:
    """记录一次工具调用。

    args_keys 只记参数名不记参数值，避免把订单号、用户输入等写进日志。
    ok=False 时 error_type 记录异常类型（或领域错误码）。
    """
    fields = {
        "tool": tool,
        "args_keys": args_keys,
        "ok": ok,
        "latency_ms": round(latency_ms, 2),
    }
    if error_type:
        fields["error_type"] = error_type
    log_event("tool_call", **fields)

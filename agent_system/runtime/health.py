# agent_system/runtime/health.py
# 健康检查：/health 存活检查、/health/ready 就绪检查（不访问 LLM）。
# 为什么要区分两种：存活检查回答「进程还活着吗」，就绪检查回答「现在能正常服务吗」；
# 就绪检查依赖外部资源，失败时返回 503，让负载均衡把流量摘走。

from fastapi import Request
from fastapi.responses import JSONResponse

from agent_system.config import get_settings
from agent_system.obs.events import health_check
from agent_system.obs.request_id import get_request_id


def _request_id(request: Request) -> str:
    """取请求 ID：优先 scope state，其次 contextvars，最后兜底 "-"。"""
    return getattr(request.state, "request_id", None) or get_request_id() or "-"


def check_redis(settings) -> dict:
    """Redis 连通性检查；未配置视为不检查，不影响就绪。

    返回 {"ok": bool, "detail": str}；detail 只给简短原因（pong/timeout/unreachable），
    不包含连接串，避免泄露地址与凭据。
    """
    if not settings.redis_url:
        return {"ok": True, "detail": "not_configured"}
    try:
        import redis
    except ImportError:  # pragma: no cover - 依赖缺失的极端环境
        return {"ok": False, "detail": "redis package missing"}

    client = None
    try:
        # 1 秒超时：健康检查本身不能变成慢请求
        client = redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        client.ping()
        return {"ok": True, "detail": "pong"}
    except Exception as exc:  # noqa: BLE001 - 健康检查需要吞掉所有异常
        if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
            return {"ok": False, "detail": "timeout"}
        return {"ok": False, "detail": "unreachable"}
    finally:
        # 无论成功失败都关闭连接，避免每次探针都泄漏一个客户端
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def check_data_dir() -> dict:
    """仅检查 data/*.json 是否可读，不发起任何外部请求。"""
    try:
        from agent_system.datasource import DATA_DIR
    except Exception:  # noqa: BLE001
        return {"ok": False, "detail": "datasource unavailable"}

    if not DATA_DIR.is_dir():
        return {"ok": False, "detail": "data dir missing"}

    files = sorted(DATA_DIR.glob("*.json"))
    unreadable = 0
    for path in files:
        # 只读 1 个字节：足以验证文件存在且可读，又不把大文件读进内存
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except OSError:
            unreadable += 1

    if unreadable:
        return {"ok": False, "detail": f"{unreadable} files unreadable"}
    return {"ok": True, "detail": f"{len(files)} files readable"}


async def liveness(request: Request) -> JSONResponse:
    """存活检查：只要进程能响应就返回 200，不检查任何外部依赖。"""
    request_id = _request_id(request)
    service = get_settings().service_name
    health_check("liveness", "ok")
    return JSONResponse(
        {"status": "ok", "service": service, "request_id": request_id}
    )


async def readiness(request: Request) -> JSONResponse:
    """就绪检查：汇总 Redis 与数据目录状态，任一关键项失败则返回 503。

    状态语义：
        ok       -> 全部正常
        degraded -> Redis 不可用（仍可服务，只是多轮上下文降级）
        down     -> 数据目录不可读（工具无法工作，不应接流量）
    """
    settings = get_settings()
    checks = {
        "redis": check_redis(settings),
        "data": check_data_dir(),
    }
    if not checks["data"]["ok"]:
        status = "down"
    elif not checks["redis"]["ok"]:
        status = "degraded"
    else:
        status = "ok"

    health_check("readiness", status, checks)
    return JSONResponse(
        {
            "status": status,
            "checks": checks,
            "service": settings.service_name,
            "request_id": _request_id(request),
        },
        status_code=200 if status == "ok" else 503,
    )


def register_health_routes(app) -> None:
    """注册两个健康检查路由；include_in_schema=False 避免出现在 OpenAPI 文档里。"""
    app.add_api_route("/health", liveness, methods=["GET"], include_in_schema=False)
    app.add_api_route("/health/ready", readiness, methods=["GET"], include_in_schema=False)

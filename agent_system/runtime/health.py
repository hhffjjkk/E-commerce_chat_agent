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

    返回:
        {"ok": bool, "detail": str}；detail 只给简短原因码
        （pong / not_configured / timeout / connect_error / ping_failed），
        不包含连接串，避免泄露地址与凭据。

    说明:
        - 复用 RedisManager 的连接池，不再每次探针新建客户端并关闭；
        - 使用 ping(force=True)：健康检查要求实时性，不接受 5 秒 PING 缓存窗口，
          否则 Redis 刚挂掉时 /health/ready 可能仍然报 ok；
        - 响应结构保持不变（checks.redis.ok / detail），仅内部实现调整。
    """
    if not settings.redis_url:
        return {"ok": True, "detail": "not_configured"}

    # 延迟导入：health 模块在应用初始化早期被导入，避免 cache -> config 的循环风险
    from agent_system.cache import get_redis_manager

    manager = get_redis_manager()
    if manager.ping(force=True):
        return {"ok": True, "detail": "pong"}
    # 失败原因由 Manager 归类（timeout / connect_error / ping_failed），只记录原因码
    return {"ok": False, "detail": manager.last_error or "ping_failed"}


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

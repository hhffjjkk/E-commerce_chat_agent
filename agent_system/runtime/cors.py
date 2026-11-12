# agent_system/runtime/cors.py
# CORS 配置：默认关闭（空列表不注册中间件），避免默认放宽跨域。

from fastapi.middleware.cors import CORSMiddleware

from agent_system.obs.events import log_event


def configure_cors(app, settings) -> bool:
    """按配置注册 CORSMiddleware；返回是否注册。

    参数：
        app: 被包装的 FastAPI 实例。
        settings: agent_system.config.Settings，读取 cors_allow_origins。

    返回：
        True 表示已注册，False 表示未配置白名单（保持默认的同源策略）。
    """
    origins = [origin for origin in settings.cors_allow_origins if origin]
    if not origins:
        return False

    if "*" in origins:
        # 允许通配但固定 allow_credentials=False，避免 * 与凭证组合的安全风险
        log_event(
            "cors_warning",
            allow_origins=origins,
            allow_credentials=False,
            message="CORS_ALLOW_ORIGINS 含通配符 *，已强制禁用凭证",
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    )
    return True

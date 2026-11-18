# agent_system/config.py
# 最小配置：读取环境变量。
# 设计原则：
#   1. 不引入 pydantic-settings 等新依赖，全部用 os.getenv + 手写解析；
#   2. 任何解析失败都回落到默认值，配置写错也不该让服务起不来。

import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 项目根目录（agent_system/ 的上一级），用于定位 data/、static/、evals/ 等资源
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 各配置项默认值集中放置，便于和文档里的配置表对照
_DEFAULT_MAX_BODY_BYTES = 65536
_DEFAULT_WINDOW_SECONDS = 60.0
_DEFAULT_MAX_REQUESTS = 120


def _normalize_level(value: str) -> int:
    """把 "info" / "DEBUG" 之类的字符串转成 logging 的数字级别；非法值回落 INFO。"""
    level = getattr(logging, value.upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量，兼容 1/true/yes/on 与 0/false/no/off；其余按默认值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(
    name: str, default: int, *, minimum: int = 1, invalid: list[str] | None = None
) -> int:
    """读取整型环境变量；非数字或小于下限时回落默认值。

    参数:
        name: 环境变量名。
        default: 解析失败时的默认值。
        minimum: 允许的最小值（含）。
        invalid: 可选列表；解析失败时把变量名追加进去，供 config_invalid 事件使用
            （只记录变量名，不记录值，避免把错误配置内容写进日志）。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        if invalid is not None:
            invalid.append(name)
        return default
    if value < minimum:
        if invalid is not None:
            invalid.append(name)
        return default
    return value


# 多租户 Key 前缀允许的字符：字母数字下划线连字符与冒号。
# 允许冒号是为了支持分层前缀（如 tenantA:prod），同时排除空格与其它分隔符，
# 避免生成无法排查的“隐形 key”。
_KEY_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9_:-]{1,32}$")


def _env_key_prefix(name: str, invalid: list[str] | None = None) -> str:
    """读取 Redis Key 前缀：空或非法时返回空串，表示不加前缀（向后兼容）。

    参数:
        name: 环境变量名。
        invalid: 可选列表；非法值只记录变量名，不记录值。

    返回:
        合法前缀（不含首尾冒号）或空串。
    """
    raw = os.getenv(name)
    if raw is None:
        return ""
    value = raw.strip().strip(":")
    if not value:
        return ""
    if not _KEY_PREFIX_PATTERN.fullmatch(value):
        if invalid is not None:
            invalid.append(name)
        return ""
    return value


def _env_float(
    name: str, default: float, *, minimum: float = 0.001, invalid: list[str] | None = None
) -> float:
    """读取浮点型环境变量；非数字或小于下限时回落默认值。

    参数:
        name: 环境变量名。
        default: 解析失败时的默认值。
        minimum: 允许的最小值（含）。
        invalid: 可选列表；解析失败时记录变量名（不记录值）。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        if invalid is not None:
            invalid.append(name)
        return default
    if value < minimum:
        if invalid is not None:
            invalid.append(name)
        return default
    return value


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    """读取逗号分隔的列表型环境变量，去掉空白项并返回不可变元组。"""
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    """全部运行时配置的快照；frozen=True 保证运行期不被意外改写。"""

    service_name: str
    log_level: int
    log_level_name: str
    log_format: str
    # 跨域白名单；为空表示不注册 CORS 中间件（默认不放宽跨域）
    cors_allow_origins: tuple[str, ...]
    rate_limit_enabled: bool
    rate_limit_window_seconds: float
    rate_limit_max_requests: int
    # 不计入限流的路径，默认排除健康检查，避免探针把配额吃满
    rate_limit_exclude_paths: tuple[str, ...]
    max_body_bytes: int
    redis_url: str | None
    # Redis 连接池与降级（阶段五步骤 1；REDIS_URL 为空时以下参数不生效）
    redis_socket_timeout: float
    redis_connect_timeout: float
    redis_max_connections: int
    redis_health_check_interval: int
    redis_ping_cache_seconds: float
    redis_fallback_to_memory: bool
    # 多租户 Key 前缀与 TTL（阶段五步骤 2）
    redis_key_prefix: str
    redis_session_ttl_seconds: int
    redis_session_state_ttl_seconds: int
    # pending store 后端与 TTL（阶段五步骤 3）
    pending_store_backend: str
    pending_lock_ttl_seconds: int
    pending_redis_ttl_seconds: int
    # 归一化消息缓存（阶段五步骤 4；默认关闭，保证既有行为零变化）
    nmsg_cache_enabled: bool
    nmsg_cache_enabled_profiles: tuple[str, ...]
    nmsg_ttl_seconds: int
    nmsg_order_ttl_seconds: int
    nmsg_min_length: int
    nmsg_stream_chunk_size: int
    # Langfuse 观测层配置：密钥为空时整体 no-op，不影响业务
    langfuse_enabled: bool
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_base_url: str
    # 上下文组装预算（同时由 ContextPolicy.from_env 读取，便于测试独立注入）
    context_max_history: int
    context_max_total_tokens: int
    context_reserve_completion_tokens: int
    context_min_keep_recent: int
    context_max_user_chars: int
    context_stable_prefix: bool
    # 工具循环策略（同时由 LoopPolicy.from_env 读取，便于测试注入）
    loop_max_steps: int
    loop_max_tool_calls: int
    loop_timeout_seconds: float
    loop_enable_interrupt_check: bool
    # 写操作两阶段确认
    confirmation_ttl_seconds: int
    confirmation_enabled: bool
    confirmation_max_pending_per_session: int
    # 过期判断的时钟容差（秒）：只在判断侧放宽，不改写 expires_at
    confirmation_clock_skew_seconds: float
    # Retry / Fallback（默认关闭，保证既有行为不变）
    retry_enabled: bool
    retry_max_attempts: int
    retry_initial_backoff_ms: int
    retry_max_backoff_ms: int
    retry_backoff_multiplier: float
    retry_jitter_ratio: float
    retry_llm: bool
    retry_tools: bool
    retry_fallback_enabled: bool
    retry_fallback_text: str
    # Router（本阶段只读配置，接入在阶段四步骤 3）
    router_strategy: str
    router_confidence_threshold: float
    router_default_profile: str
    router_llm_enabled: bool
    router_llm_timeout_seconds: float


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存配置。lru_cache 保证整个进程只解析一次环境变量。"""
    service_name = os.getenv("SERVICE_NAME", "ecommerce-agent").strip() or "ecommerce-agent"
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    log_format = os.getenv("LOG_FORMAT", "json").strip().lower()
    if log_format not in {"json", "text"}:
        log_format = "json"

    # 记录非法配置项名称（不记录值）；仅用于启动时可见，不阻塞服务
    invalid_env: list[str] = []

    pending_backend = (os.getenv("PENDING_STORE_BACKEND", "").strip().lower() or "auto")
    if pending_backend not in {"auto", "memory", "redis"}:
        # 未知后端名按 auto 处理，避免拼写错误让服务起不来
        invalid_env.append("PENDING_STORE_BACKEND")
        pending_backend = "auto"

    parsed = Settings(
        service_name=service_name,
        log_level=_normalize_level(level_name),
        log_level_name=level_name,
        log_format=log_format,
        cors_allow_origins=_env_list("CORS_ALLOW_ORIGINS"),
        rate_limit_enabled=_env_bool("RATE_LIMIT_ENABLED", True),
        rate_limit_window_seconds=_env_float("RATE_LIMIT_WINDOW_SECONDS", _DEFAULT_WINDOW_SECONDS),
        rate_limit_max_requests=_env_int("RATE_LIMIT_MAX_REQUESTS", _DEFAULT_MAX_REQUESTS),
        rate_limit_exclude_paths=_env_list("RATE_LIMIT_EXCLUDE_PATHS", "/health,/health/ready"),
        max_body_bytes=_env_int("MAX_BODY_BYTES", _DEFAULT_MAX_BODY_BYTES),
        redis_url=os.getenv("REDIS_URL") or None,
        # 1 秒超时与“缓存可丢失、可重试”的语义匹配；连接数默认 50 够单实例使用
        redis_socket_timeout=_env_float("REDIS_SOCKET_TIMEOUT", 1.0),
        redis_connect_timeout=_env_float("REDIS_CONNECT_TIMEOUT", 1.0),
        redis_max_connections=_env_int("REDIS_MAX_CONNECTIONS", 50),
        # 0 表示关闭 redis-py 的内建空闲连接探活
        redis_health_check_interval=_env_int(
            "REDIS_HEALTH_CHECK_INTERVAL", 30, minimum=0
        ),
        # 5 秒 PING 缓存：避免每个请求入口都真 PING 造成 QPS 翻倍
        redis_ping_cache_seconds=_env_float("REDIS_PING_CACHE_SECONDS", 5.0),
        # 默认不自动回退：显式失败让 Redis 故障在健康检查与日志里可见
        redis_fallback_to_memory=_env_bool("REDIS_FALLBACK_TO_MEMORY", False),
        # 多租户前缀默认为空：不改变既有 key（chat:{sid}），保证向后兼容
        redis_key_prefix=_env_key_prefix("REDIS_KEY_PREFIX", invalid_env),
        # 历史消息 TTL 默认 0=不过期：现有部署依赖历史消息持久化
        redis_session_ttl_seconds=_env_int(
            "REDIS_SESSION_TTL_SECONDS", 0, minimum=0, invalid=invalid_env
        ),
        # 会话状态是短周期元数据，默认 30 分钟，让 Redis 内存可控
        redis_session_state_ttl_seconds=_env_int(
            "REDIS_SESSION_STATE_TTL_SECONDS", 1800, minimum=1, invalid=invalid_env
        ),
        # pending 存储后端：auto 默认；memory/redis 为显式强制
        pending_store_backend=pending_backend,
        # 锁 TTL 默认 10 秒，远大于 JSON 写操作耗时，又远小于业务 TTL
        pending_lock_ttl_seconds=_env_int(
            "PENDING_LOCK_TTL_SECONDS", 10, minimum=1, invalid=invalid_env
        ),
        # Redis 层 TTL 默认 3600；构造期还会校验 >= 2 × CONFIRMATION_TTL_SECONDS
        pending_redis_ttl_seconds=_env_int(
            "PENDING_REDIS_TTL_SECONDS", 3600, minimum=1, invalid=invalid_env
        ),
        # 默认关闭：归一化消息缓存会跳过 LLM 调用，必须由部署方显式开启
        nmsg_cache_enabled=_env_bool("NMSG_CACHE_ENABLED", False),
        # 白名单默认只含读多写少的售前与订单；after_sales 涉及确认流程，永不默认开启
        nmsg_cache_enabled_profiles=_env_list(
            "NMSG_CACHE_ENABLED_PROFILES", "pre_sales,order"
        ),
        nmsg_ttl_seconds=_env_int(
            "NMSG_TTL_SECONDS", 3600, minimum=1, invalid=invalid_env
        ),
        # 订单状态变化快：默认 60 秒，避免把“已发货”当“待发货”返回
        nmsg_order_ttl_seconds=_env_int(
            "NMSG_ORDER_TTL_SECONDS", 60, minimum=1, invalid=invalid_env
        ),
        nmsg_min_length=_env_int(
            "NMSG_MIN_LENGTH", 4, minimum=1, invalid=invalid_env
        ),
        nmsg_stream_chunk_size=_env_int(
            "NMSG_STREAM_CHUNK_SIZE", 20, minimum=1, invalid=invalid_env
        ),
        langfuse_enabled=_env_bool("LANGFUSE_ENABLED", True),
        langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY") or None,
        langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY") or None,
        # v3 使用 LANGFUSE_BASE_URL；兼容 v2 的 LANGFUSE_HOST 写法
        langfuse_base_url=(
            os.getenv("LANGFUSE_BASE_URL", "").strip()
            or os.getenv("LANGFUSE_HOST", "").strip()
            or "http://localhost:3000"
        ),
        context_max_history=_env_int("CONTEXT_MAX_HISTORY", 20),
        context_max_total_tokens=_env_int("CONTEXT_MAX_TOTAL_TOKENS", 8000),
        context_reserve_completion_tokens=_env_int("CONTEXT_RESERVE_COMPLETION", 1500),
        context_min_keep_recent=_env_int("CONTEXT_MIN_KEEP_RECENT", 4),
        context_max_user_chars=_env_int("CONTEXT_MAX_USER_CHARS", 4000),
        context_stable_prefix=_env_bool("CONTEXT_STABLE_PREFIX", True),
        loop_max_steps=_env_int("LOOP_MAX_STEPS", 5),
        loop_max_tool_calls=_env_int("LOOP_MAX_TOOL_CALLS", 20),
        loop_timeout_seconds=_env_float("LOOP_TIMEOUT_SECONDS", 120.0),
        loop_enable_interrupt_check=_env_bool("LOOP_ENABLE_INTERRUPT_CHECK", False),
        confirmation_ttl_seconds=_env_int("CONFIRMATION_TTL_SECONDS", 300),
        confirmation_enabled=_env_bool("CONFIRMATION_ENABLED", True),
        confirmation_max_pending_per_session=_env_int(
            "CONFIRMATION_MAX_PENDING_PER_SESSION", 5
        ),
        # 5 秒容差相对 300 秒 TTL 仅 1.7%：足以吸收 NTP 跳变，又不明显延长过期窗口
        confirmation_clock_skew_seconds=_env_float(
            "CONFIRMATION_CLOCK_SKEW_SECONDS", 5.0, minimum=0.0, invalid=invalid_env
        ),
        retry_enabled=_env_bool("RETRY_ENABLED", False),
        retry_max_attempts=_env_int("RETRY_MAX_ATTEMPTS", 3),
        retry_initial_backoff_ms=_env_int("RETRY_INITIAL_BACKOFF_MS", 300),
        retry_max_backoff_ms=_env_int("RETRY_MAX_BACKOFF_MS", 5000),
        retry_backoff_multiplier=_env_float(
            "RETRY_BACKOFF_MULTIPLIER", 2.0, minimum=1.0
        ),
        retry_jitter_ratio=_env_float("RETRY_JITTER_RATIO", 0.2, minimum=0.0),
        retry_llm=_env_bool("RETRY_LLM", True),
        retry_tools=_env_bool("RETRY_TOOLS", True),
        retry_fallback_enabled=_env_bool("RETRY_FALLBACK_ENABLED", True),
        retry_fallback_text=(
            os.getenv("RETRY_FALLBACK_TEXT", "").strip() or "系统繁忙，请稍后重试。"
        ),
        router_strategy=os.getenv("ROUTER_STRATEGY", "").strip() or "rule_first",
        router_confidence_threshold=_env_float(
            "ROUTER_CONFIDENCE_THRESHOLD", 0.55, minimum=0.0
        ),
        router_default_profile=(
            os.getenv("ROUTER_DEFAULT_PROFILE", "").strip() or "pre_sales"
        ),
        router_llm_enabled=_env_bool("ROUTER_LLM_ENABLED", True),
        router_llm_timeout_seconds=_env_float(
            "ROUTER_LLM_TIMEOUT_SECONDS", 5.0, minimum=0.001
        ),
    )

    if invalid_env:
        # 只记录变量名（沿用各 Policy 的 invalid_env 口径），不记录错误值
        try:
            from agent_system.obs.events import log_event

            log_event("config_invalid", invalid_env=invalid_env)
        except Exception:  # noqa: BLE001 - 日志失败不能影响配置读取
            pass

    return parsed

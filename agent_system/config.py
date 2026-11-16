# agent_system/config.py
# 最小配置：读取环境变量。
# 设计原则：
#   1. 不引入 pydantic-settings 等新依赖，全部用 os.getenv + 手写解析；
#   2. 任何解析失败都回落到默认值，配置写错也不该让服务起不来。

import logging
import os
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


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取整型环境变量；非数字或小于下限时回落默认值（避免 0/负数造成异常行为）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.001) -> float:
    """读取浮点型环境变量；非数字或小于下限时回落默认值（窗口时间不能为 0）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取并缓存配置。lru_cache 保证整个进程只解析一次环境变量。"""
    service_name = os.getenv("SERVICE_NAME", "ecommerce-agent").strip() or "ecommerce-agent"
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    log_format = os.getenv("LOG_FORMAT", "json").strip().lower()
    if log_format not in {"json", "text"}:
        log_format = "json"

    return Settings(
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
    )

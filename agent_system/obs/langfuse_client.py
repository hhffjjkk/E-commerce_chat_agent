"""Langfuse 客户端初始化与 no-op 降级。

本模块是观测层（Harness 实现之一）的接入点：
- 未配置密钥或未安装 SDK 时，对外表现为 `NoOpLangfuse`，业务行为与步骤 4 完全一致；
- 启用时使用 Langfuse Python SDK v3 的单例客户端，并在启动阶段做一次 `auth_check()`；
- 任何初始化/鉴权失败都只记录日志并降级为 no-op，绝不让服务启动失败。
"""

from agent_system.config import get_settings
from agent_system.obs.events import log_event

# 单例状态：enabled 与 client 始终成对更新，避免出现“已标记启用但无客户端”
_client = None
_enabled = False


class NoOpLangfuse:
    """未启用 Langfuse 时的占位客户端：所有方法均为 no-op 并返回 None。

    设计目的：
        让映射层与评估层不需要到处写 `if enabled`，直接调用即可；任何未知方法
        也通过 `__getattr__` 兜底为 no-op，避免 SDK 版本差异导致 AttributeError。
    """

    def auth_check(self):
        """占位鉴权检查：未启用时直接返回 None。"""
        return None

    def start_observation(self, *args, **kwargs):
        """占位 span 创建：未启用时不做任何事。"""
        return None

    def start_as_current_observation(self, *args, **kwargs):
        """占位上下文 span：未启用时不做任何事。"""
        return None

    def update_current_span(self, *args, **kwargs):
        """占位当前 span 更新：未启用时不做任何事。"""
        return None

    def update_current_generation(self, *args, **kwargs):
        """占位当前 generation 更新：未启用时不做任何事。"""
        return None

    def score(self, *args, **kwargs):
        """占位评分上报：未启用时不做任何事。"""
        return None

    def flush(self, *args, **kwargs):
        """占位刷新：未启用时不做任何事。"""
        return None

    def shutdown(self, *args, **kwargs):
        """占位关闭：未启用时不做任何事。"""
        return None

    def __getattr__(self, name):
        """未知方法统一返回 no-op，保证降级路径不会因 SDK 差异报错。"""

        def _noop(*args, **kwargs):
            return None

        return _noop


_NOOP = NoOpLangfuse()


def is_enabled() -> bool:
    """返回 Langfuse 是否已成功启用（初始化成功且鉴权通过）。

    返回:
        True 表示可以调用真实 SDK；False 表示当前为 no-op 模式。
    """
    return _enabled


def get_langfuse():
    """返回当前可用的 Langfuse 客户端。

    返回:
        启用时为真实 SDK 客户端；未启用时为 NoOpLangfuse（不会返回 None），
        调用方无需做空值判断。
    """
    if _enabled and _client is not None:
        return _client
    return _NOOP


def _create_client(settings):
    """按 v3 规范构造 Langfuse 客户端。

    参数:
        settings: 运行时配置快照，包含公钥、私钥与自托管地址。

    返回:
        Langfuse SDK 客户端实例。

    异常:
        ImportError: 未安装 langfuse 依赖。
        Exception: SDK 构造失败时向上抛出，由 init_langfuse 统一降级。
    """
    # 延迟导入：未安装 SDK 时整个服务仍可启动（no-op）
    from langfuse import Langfuse

    try:
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_base_url,
        )
    except TypeError:
        # 兼容部分版本使用 base_url 命名的构造参数
        return Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
        )


def init_langfuse(settings=None) -> bool:
    """初始化 Langfuse 客户端；失败时安全降级为 no-op。

    参数:
        settings: 可选配置快照；缺省时读取 get_settings()。

    返回:
        True 表示已启用并鉴权通过；False 表示 no-op（未配置/总开关关闭/鉴权失败）。

    说明:
        该函数可重复调用（测试场景），每次都会重置单例状态。
    """
    global _client, _enabled
    settings = settings or get_settings()

    keys_present = bool(settings.langfuse_public_key and settings.langfuse_secret_key)
    if not settings.langfuse_enabled or not keys_present:
        _client = None
        _enabled = False
        log_event(
            "langfuse_init",
            enabled=False,
            reason="disabled" if not settings.langfuse_enabled else "not_configured",
        )
        return False

    try:
        client = _create_client(settings)
        # auth_check 失败可能返回 False 或抛异常，两种都视为不可用
        if client.auth_check() is False:
            raise RuntimeError("auth_check returned False")
    except Exception as exc:  # noqa: BLE001 - 观测层失败不能影响启动
        # 尽量释放 SDK 的后台线程/连接，避免降级后仍有残留资源
        if "client" in locals() and client is not None:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001
                pass
        _client = None
        _enabled = False
        log_event("langfuse_auth_failed", error_type=type(exc).__name__)
        log_event(
            "langfuse_init",
            enabled=False,
            reason="auth_failed",
            base_url=settings.langfuse_base_url,
        )
        return False

    _client = client
    _enabled = True
    # 只记录地址与开关，不记录任何密钥
    log_event("langfuse_init", enabled=True, base_url=settings.langfuse_base_url)
    return True

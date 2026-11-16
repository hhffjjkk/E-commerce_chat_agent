# agent_system/harness/retry_policy.py
# Retry 策略：默认关闭，显式开启后才对可重试异常做指数退避。
# 默认关闭的原因：保证既有 135 项测试与线上行为零变化，避免重试放大上游故障。

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

_ENV_ENABLED = "RETRY_ENABLED"
_ENV_MAX_ATTEMPTS = "RETRY_MAX_ATTEMPTS"
_ENV_INITIAL_BACKOFF = "RETRY_INITIAL_BACKOFF_MS"
_ENV_MAX_BACKOFF = "RETRY_MAX_BACKOFF_MS"
_ENV_MULTIPLIER = "RETRY_BACKOFF_MULTIPLIER"
_ENV_JITTER = "RETRY_JITTER_RATIO"
_ENV_RETRY_LLM = "RETRY_LLM"
_ENV_RETRY_TOOLS = "RETRY_TOOLS"
_ENV_FALLBACK_ENABLED = "RETRY_FALLBACK_ENABLED"
_ENV_FALLBACK_TEXT = "RETRY_FALLBACK_TEXT"

_DEFAULT_FALLBACK_TEXT = "系统繁忙，请稍后重试。"


@dataclass(frozen=True)
class RetryPolicy:
    """Retry / Fallback 策略参数。

    字段说明：
        enabled: 总开关；默认 False，保证默认行为与之前逐字节一致。
        max_attempts: 含首次，默认 3（即最多重试 2 次）。
        initial_backoff_ms: 首次退避毫秒数。
        max_backoff_ms: 单次退避上限。
        backoff_multiplier: 指数因子（>=1）。
        jitter_ratio: 抖动比例 0~1，避免多实例同时重试形成脉冲。
        retry_llm: 开启时是否重试 LLM 调用。
        retry_tools: 开启时是否重试幂等读工具。
        fallback_enabled: LLM 重试耗尽后是否返回降级文本。
        fallback_text: 降级文本（不写入日志）。
    """

    enabled: bool = False
    max_attempts: int = 3
    initial_backoff_ms: int = 300
    max_backoff_ms: int = 5000
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.2
    retry_llm: bool = True
    retry_tools: bool = True
    fallback_enabled: bool = True
    fallback_text: str = _DEFAULT_FALLBACK_TEXT

    def validate(self) -> None:
        """校验策略参数，非法组合抛中文 ValueError。"""
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError(f"max_attempts 必须 >= 1，当前值: {self.max_attempts!r}")
        if self.initial_backoff_ms <= 0:
            raise ValueError("initial_backoff_ms 必须为正数")
        if self.max_backoff_ms < self.initial_backoff_ms:
            raise ValueError("max_backoff_ms 不能小于 initial_backoff_ms")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier 必须 >= 1.0")
        if not 0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio 必须在 0~1 之间")
        if not str(self.fallback_text or "").strip():
            raise ValueError("fallback_text 不能为空")

    def policy_id(self) -> str:
        """返回策略字段哈希前 8 位（不含 fallback_text，避免文本影响分组）。"""
        raw = "|".join(
            [
                str(self.enabled),
                str(self.max_attempts),
                str(self.initial_backoff_ms),
                str(self.max_backoff_ms),
                str(self.backoff_multiplier),
                str(self.jitter_ratio),
                str(self.retry_llm),
                str(self.retry_tools),
                str(self.fallback_enabled),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def from_env(cls) -> tuple["RetryPolicy", list[str]]:
        """从 RETRY_* 环境变量构造策略。

        返回:
            (policy, invalid_env)。非法值回退默认，invalid_env 只含变量名。

        说明:
            fallback_text 为空时回退默认文本，避免降级时返回空字符串。
        """
        invalid: list[str] = []
        fallback_text = os.getenv(_ENV_FALLBACK_TEXT, "")
        if not fallback_text.strip():
            if _ENV_FALLBACK_TEXT in os.environ:
                invalid.append(_ENV_FALLBACK_TEXT)
            fallback_text = _DEFAULT_FALLBACK_TEXT

        policy = RetryPolicy(
            enabled=_read_bool(_ENV_ENABLED, False, invalid),
            max_attempts=_read_int(_ENV_MAX_ATTEMPTS, 3, invalid),
            initial_backoff_ms=_read_int(_ENV_INITIAL_BACKOFF, 300, invalid),
            max_backoff_ms=_read_int(_ENV_MAX_BACKOFF, 5000, invalid),
            backoff_multiplier=_read_float(_ENV_MULTIPLIER, 2.0, invalid),
            jitter_ratio=_read_float(_ENV_JITTER, 0.2, invalid),
            retry_llm=_read_bool(_ENV_RETRY_LLM, True, invalid),
            retry_tools=_read_bool(_ENV_RETRY_TOOLS, True, invalid),
            fallback_enabled=_read_bool(_ENV_FALLBACK_ENABLED, True, invalid),
            fallback_text=fallback_text,
        )
        return policy, invalid


def _read_int(name: str, default: int, invalid: list[str]) -> int:
    """读取正整数；缺失/非法/非正数记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        invalid.append(name)
        return default
    if value <= 0:
        invalid.append(name)
        return default
    return value


def _read_float(name: str, default: float, invalid: list[str]) -> float:
    """读取非负浮点数；缺失/非法记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        invalid.append(name)
        return default
    if value < 0:
        invalid.append(name)
        return default
    return value


def _read_bool(name: str, default: bool, invalid: list[str]) -> bool:
    """读取布尔值；非法值记录后回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    invalid.append(name)
    return default


_policy: RetryPolicy | None = None


def get_retry_policy() -> RetryPolicy:
    """返回全局 RetryPolicy 单例；首次调用时读取环境变量并记录 retry_policy_loaded。"""
    global _policy
    if _policy is None:
        from agent_system.obs.events import log_event

        policy, invalid_env = RetryPolicy.from_env()
        policy.validate()
        _policy = policy
        # 不记录 fallback_text，避免把可配置文案（可能含业务信息）写进日志
        log_event(
            "retry_policy_loaded",
            enabled=policy.enabled,
            max_attempts=policy.max_attempts,
            initial_backoff_ms=policy.initial_backoff_ms,
            max_backoff_ms=policy.max_backoff_ms,
            backoff_multiplier=policy.backoff_multiplier,
            jitter_ratio=policy.jitter_ratio,
            retry_llm=policy.retry_llm,
            retry_tools=policy.retry_tools,
            fallback_enabled=policy.fallback_enabled,
            policy_id=policy.policy_id(),
            invalid_env=invalid_env,
        )
    return _policy


def reset_retry_policy() -> None:
    """重置全局 RetryPolicy（仅供测试隔离；生产代码不得调用）。"""
    global _policy
    _policy = None

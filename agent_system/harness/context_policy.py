# agent_system/harness/context_policy.py
# 上下文组装策略参数：全部冻结，运行期不可突变。
# 环境变量解析失败时回退默认值，并把非法项交给调用方记录日志。

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# 环境变量名集中定义，避免拼写漂移
_ENV_MAX_HISTORY = "CONTEXT_MAX_HISTORY"
_ENV_MAX_TOTAL_TOKENS = "CONTEXT_MAX_TOTAL_TOKENS"
_ENV_RESERVE_COMPLETION = "CONTEXT_RESERVE_COMPLETION"
_ENV_MIN_KEEP_RECENT = "CONTEXT_MIN_KEEP_RECENT"
_ENV_MAX_USER_CHARS = "CONTEXT_MAX_USER_CHARS"
_ENV_STABLE_PREFIX = "CONTEXT_STABLE_PREFIX"


@dataclass(frozen=True)
class ContextPolicy:
    """上下文组装策略参数。

    字段说明：
        max_history_messages: 进入组装层的最大历史条数（与 memory 的存储上限独立）。
        max_total_tokens: 组装后 messages 的总预算（近似估算）。
        reserve_completion_tokens: 为模型输出预留的 token（估算值，不保证精确）。
        min_keep_recent_messages: 无论如何保留的最近历史条数。
        max_user_message_chars: 单条 user 消息的字符硬上限。
        enable_truncation_marker: 是否在丢弃历史时插入压缩标记消息。
        stable_prefix_mode: 是否把工具 schema 纳入稳定前缀哈希。
    """

    max_history_messages: int = 20
    max_total_tokens: int = 8000
    reserve_completion_tokens: int = 1500
    min_keep_recent_messages: int = 4
    max_user_message_chars: int = 4000
    enable_truncation_marker: bool = True
    stable_prefix_mode: bool = True

    def validate(self) -> None:
        """校验策略参数是否自洽。

        异常:
            ValueError: 任一整数非正、预算空间不足、保底条数大于窗口上限。

        说明:
            预算是近似值，但仍要求 max_total_tokens 至少比输出预留多 200，
            否则连 system + user 的最小空间都无法容纳。
        """
        integer_fields = {
            "max_history_messages": self.max_history_messages,
            "max_total_tokens": self.max_total_tokens,
            "reserve_completion_tokens": self.reserve_completion_tokens,
            "min_keep_recent_messages": self.min_keep_recent_messages,
            "max_user_message_chars": self.max_user_message_chars,
        }
        for name, value in integer_fields.items():
            # bool 是 int 的子类，这里不接受 True/False 之类的配置
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} 必须为正整数，当前值: {value!r}")

        # 至少留 200 token 给 system + user，避免预算被输出预留吃光
        if self.max_total_tokens <= self.reserve_completion_tokens + 200:
            raise ValueError(
                "max_total_tokens 必须大于 reserve_completion_tokens + 200"
                f"（当前 {self.max_total_tokens} <= {self.reserve_completion_tokens + 200}）"
            )

        # 保底条数不能超过窗口上限，否则窗口裁剪后永远达不到保底
        if self.min_keep_recent_messages > self.max_history_messages:
            raise ValueError(
                "min_keep_recent_messages 不能大于 max_history_messages"
                f"（当前 {self.min_keep_recent_messages} > {self.max_history_messages}）"
            )

    def policy_id(self) -> str:
        """返回策略的稳定短哈希（8 位十六进制）。

        返回:
            sha256(字段拼接) 的前 8 位，用于阶段六按策略分组做对比评测。

        说明:
            仅使用策略字段本身，不含运行期环境信息，保证同配置同 ID。
        """
        raw = "|".join(
            [
                str(self.max_history_messages),
                str(self.max_total_tokens),
                str(self.reserve_completion_tokens),
                str(self.min_keep_recent_messages),
                str(self.max_user_message_chars),
                str(self.enable_truncation_marker),
                str(self.stable_prefix_mode),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def from_env(cls) -> "ContextPolicy":
        """从环境变量构造策略；非法值回退默认值且不抛异常。

        返回:
            ContextPolicy 实例。
        """
        policy, _ = load_policy_from_env()
        return policy


def _read_int(name: str, default: int, invalid: list[str]) -> int:
    """读取整数环境变量；缺失用默认值，非法值记录后回退默认值。"""
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


def _read_bool(name: str, default: bool, invalid: list[str]) -> bool:
    """读取布尔环境变量（true/false/1/0 等）；非法值记录后回退默认值。"""
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


def load_policy_from_env() -> tuple[ContextPolicy, list[str]]:
    """读取 CONTEXT_* 环境变量并返回策略与非法项列表。

    返回:
        (policy, invalid_fields)。invalid_fields 供调用方记录
        `context_policy_loaded` 事件时使用，便于发现配置写错。

    说明:
        缺失的变量不算非法（使用默认值），只有“存在但不可解析/非正数”才记录。
    """
    invalid: list[str] = []
    policy = ContextPolicy(
        max_history_messages=_read_int(_ENV_MAX_HISTORY, 20, invalid),
        max_total_tokens=_read_int(_ENV_MAX_TOTAL_TOKENS, 8000, invalid),
        reserve_completion_tokens=_read_int(_ENV_RESERVE_COMPLETION, 1500, invalid),
        min_keep_recent_messages=_read_int(_ENV_MIN_KEEP_RECENT, 4, invalid),
        max_user_message_chars=_read_int(_ENV_MAX_USER_CHARS, 4000, invalid),
        enable_truncation_marker=True,
        stable_prefix_mode=_read_bool(_ENV_STABLE_PREFIX, True, invalid),
    )
    return policy, invalid

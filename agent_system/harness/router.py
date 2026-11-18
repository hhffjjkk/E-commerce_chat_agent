# agent_system/harness/router.py
# Router：规则优先 + LLM 兜底 + 默认 Profile 兜底的三级路由。
# 本步骤只提供 Router 本体与事件，不接入 /chat、/chat/stream 与 NDJSON。

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from agent_system.harness.agent_profile import AgentProfile, get_profile_registry
from agent_system.harness.route_rules import best_rule, score_message
from agent_system.harness.router_prompts import (
    build_router_messages,
    parse_router_response,
)
from agent_system.obs.events import log_event

_STRATEGIES = {"rule_first", "llm_first", "rule_only", "llm_only"}


class RouteStrategy(str, Enum):
    """路由决策来源。"""

    RULE = "rule"
    LLM = "llm"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class RouteDecision:
    """一次路由决策的只读结果。

    字段说明：
        agent: 最终选中的 Profile name。
        confidence: [0,1] 置信度。
        reason: 策略层原因标签（不包含用户消息/LLM 原文）。
        strategy: rule / llm / fallback。
        candidates: 候选与得分，按分数降序，用于诊断。
        latency_ms: 路由耗时。
        llm_used: 是否调用过 LLM（含失败）。
    """

    agent: str
    confidence: float
    reason: str
    strategy: RouteStrategy
    candidates: tuple[tuple[str, float], ...] = ()
    latency_ms: float = 0.0
    llm_used: bool = False


@dataclass(frozen=True)
class RoutePolicy:
    """路由策略参数。"""

    strategy: str = "rule_first"
    confidence_threshold: float = 0.55
    default_profile: str = "pre_sales"
    llm_enabled: bool = True
    llm_timeout_seconds: float = 5.0

    def validate(self) -> None:
        """校验策略枚举与数值范围。"""
        if self.strategy not in _STRATEGIES:
            raise ValueError(f"未知路由策略: {self.strategy!r}")
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold 必须在 0~1 之间")
        if not str(self.default_profile or "").strip():
            raise ValueError("default_profile 不能为空")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("llm_timeout_seconds 必须为正数")

    def policy_id(self) -> str:
        """策略字段哈希前 8 位，便于阶段六分组分析。"""
        raw = "|".join(
            [
                self.strategy,
                str(self.confidence_threshold),
                self.default_profile,
                str(self.llm_enabled),
                str(self.llm_timeout_seconds),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    @classmethod
    def from_env(cls) -> tuple["RoutePolicy", list[str]]:
        """从 ROUTER_* 环境变量构造策略，非法值回退默认。"""
        invalid: list[str] = []
        policy = RoutePolicy(
            strategy=_read_str("ROUTER_STRATEGY", "rule_first", _STRATEGIES, invalid),
            confidence_threshold=_read_float(
                "ROUTER_CONFIDENCE_THRESHOLD", 0.55, invalid, maximum=1.0
            ),
            default_profile=_read_str(
                "ROUTER_DEFAULT_PROFILE", "pre_sales", None, invalid
            ),
            llm_enabled=_read_bool("ROUTER_LLM_ENABLED", True, invalid),
            llm_timeout_seconds=_read_float(
                "ROUTER_LLM_TIMEOUT_SECONDS", 5.0, invalid, exclusive_minimum=True
            ),
        )
        return policy, invalid


def _read_str(name: str, default: str, allowed: set[str] | None, invalid: list[str]) -> str:
    """读取字符串；非法/空值/不在枚举内时回退。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if allowed is not None and value not in allowed:
        invalid.append(name)
        return default
    return value


def _read_float(
    name: str,
    default: float,
    invalid: list[str],
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    """读取浮点数；缺失/非法/越界时回退默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        invalid.append(name)
        return default
    if value < minimum or (exclusive_minimum and value == minimum):
        invalid.append(name)
        return default
    if maximum is not None and value > maximum:
        invalid.append(name)
        return default
    return value


def _read_bool(name: str, default: bool, invalid: list[str]) -> bool:
    """读取布尔值；非法值回退。"""
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


class Router:
    """三级路由执行器：规则 → LLM → 默认 Profile。

    生命周期:
        默认由 get_router() 单例持有；测试可注入 fake llm_client。
    线程安全:
        无共享可变状态；每次 route 独立计算。
    """

    def __init__(self, policy: RoutePolicy, *, llm_client: Any | None = None):
        """初始化 Router。

        参数:
            policy: 路由策略。
            llm_client: 可选的 LLM 客户端（需兼容 chat.completions.create）；
                None 且 llm_enabled=True 时尝试延迟实例化 LLMClient，
                失败视为 LLM 不可用。
        """
        policy.validate()
        self.policy = policy
        self.policy_id = policy.policy_id()
        self.llm_client = llm_client
        if self.llm_client is None and policy.llm_enabled:
            # 延迟获取，避免 Router 与 app 启动顺序强耦合；
            # 无 API Key / 导入失败都不应让路由构造失败，只是 LLM 不可用
            try:
                from agent_system.agent import LLMClient

                self.llm_client = LLMClient()
            except Exception:  # noqa: BLE001 - LLM 不可用是允许状态
                self.llm_client = None

    def route(self, message: str) -> RouteDecision:
        """对用户消息做一次路由决策。

        参数:
            message: 用户消息。

        返回:
            RouteDecision；agent 必为已注册 Profile（无 Profile 时例外）。

        说明:
            决策一次完成，不做多轮澄清；任何失败都收敛到默认 Profile。
        """
        started = time.monotonic()
        profiles = get_profile_registry().list()
        if not profiles:
            # 没有 Profile 时无候选可选，直接返回配置的默认值（可能未注册）
            return self._finish(
                agent=self.policy.default_profile,
                confidence=0.0,
                reason="no_profiles",
                strategy=RouteStrategy.FALLBACK,
                candidates=(),
                started=started,
                llm_used=False,
            )

        names = {profile.name for profile in profiles}
        default_profile = self.policy.default_profile
        invalid_default = default_profile not in names
        if invalid_default:
            # 配置的默认 Profile 不在注册表中：退到注册顺序第一个，保证仍可服务
            default_profile = profiles[0].name

        scores = score_message(message)
        rule_candidates = tuple(
            sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        )
        rule_agent, rule_conf = best_rule(message)
        llm_used = False
        llm_tag = "no_llm"

        if self.policy.strategy == "rule_only":
            if rule_agent is not None:
                return self._finish(
                    rule_agent, rule_conf, "rule_hit", RouteStrategy.RULE,
                    rule_candidates, started, llm_used,
                )
            return self._fallback(default_profile, "no_rule_hit", invalid_default,
                                  rule_candidates, started, llm_used)

        if self.policy.strategy == "llm_only":
            if not self.policy.llm_enabled or self.llm_client is None:
                return self._fallback(default_profile, "no_llm", invalid_default,
                                      rule_candidates, started, llm_used)
            agent, confidence, _, tag = self._route_with_llm(message, profiles)
            llm_used = True
            if agent is not None:
                return self._finish(
                    agent, confidence, "llm_hit", RouteStrategy.LLM,
                    self._llm_candidates(agent, confidence, default_profile),
                    started, llm_used,
                )
            return self._fallback(default_profile, _tag_reason(tag), invalid_default,
                                  rule_candidates, started, llm_used)

        if self.policy.strategy == "llm_first":
            if self.policy.llm_enabled and self.llm_client is not None:
                agent, confidence, _, tag = self._route_with_llm(message, profiles)
                llm_used = True
                if agent is not None and confidence >= self.policy.confidence_threshold:
                    return self._finish(
                        agent, confidence, "llm_hit", RouteStrategy.LLM,
                        self._llm_candidates(agent, confidence, default_profile),
                        started, llm_used,
                    )
                llm_tag = tag
            # LLM 失败或低置信：回退规则
            if rule_agent is not None:
                return self._finish(
                    rule_agent, rule_conf, "rule_hit", RouteStrategy.RULE,
                    rule_candidates, started, llm_used,
                )
            return self._fallback(default_profile, _tag_reason(llm_tag), invalid_default,
                                  rule_candidates, started, llm_used)

        # 默认 rule_first：规则高置信时零成本直接返回
        if rule_agent is not None and rule_conf >= self.policy.confidence_threshold:
            return self._finish(
                rule_agent, rule_conf, "rule_hit", RouteStrategy.RULE,
                rule_candidates, started, llm_used,
            )

        if self.policy.llm_enabled and self.llm_client is not None:
            agent, confidence, _, tag = self._route_with_llm(message, profiles)
            llm_used = True
            if agent is not None and confidence >= self.policy.confidence_threshold:
                return self._finish(
                    agent, confidence, "llm_hit", RouteStrategy.LLM,
                    self._llm_candidates(agent, confidence, default_profile),
                    started, llm_used,
                )
            # LLM 低置信或失败：保留 tag 供兜底原因使用
            llm_tag = tag if agent is None else "below_threshold"

        if rule_agent is not None:
            # 规则有命中但低于阈值，且 LLM 不可用/低置信：采用规则结果并标注
            return self._finish(
                rule_agent, rule_conf, "below_threshold", RouteStrategy.RULE,
                rule_candidates, started, llm_used,
            )
        return self._fallback(default_profile, _tag_reason(llm_tag), invalid_default,
                              rule_candidates, started, llm_used)

    def _route_with_llm(
        self,
        message: str,
        profiles: list[AgentProfile],
    ) -> tuple[str | None, float, str, str]:
        """调用 LLM 做一次路由。

        参数:
            message: 用户消息。
            profiles: 候选 Profile。

        返回:
            (agent, confidence, reason, error_tag)；error_tag 为
            ok / no_llm / timeout / parse_error / invalid_agent / exception。

        说明:
            独立轻量调用：不传 tools、不复用业务 SYSTEM_PROMPT；
            不做重试（路由已有降级路径，重试只会放大延迟）。
        """
        if self.llm_client is None:
            return None, 0.0, "", "no_llm"

        started = time.monotonic()
        raw_len = 0
        tag = "exception"
        try:
            response = self.llm_client.chat.completions.create(
                model=os.getenv("LLM_MODEL", "deepseek-chat"),
                messages=build_router_messages(message, profiles),
                temperature=0.0,
                max_tokens=200,
                timeout=self.policy.llm_timeout_seconds,
            )
            raw = response.choices[0].message.content or ""
            raw_len = len(raw)
            valid_names = {profile.name for profile in profiles}
            agent, confidence, reason = parse_router_response(raw, valid_names)
            if agent is None:
                tag = reason if reason in {"parse_error", "invalid_agent"} else "parse_error"
            else:
                tag = "ok"
        except Exception as exc:  # noqa: BLE001 - 路由失败必须降级而不是抛出
            tag = "timeout" if _is_timeout(exc) else "exception"
            agent, confidence, reason = None, 0.0, ""
        finally:
            log_event(
                "route_llm_call",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                error_tag=tag,
                raw_len=raw_len,
            )
        return agent, confidence, reason, tag

    @staticmethod
    def _llm_candidates(
        agent: str,
        confidence: float,
        default_profile: str,
    ) -> tuple[tuple[str, float], ...]:
        """LLM 路由的候选摘要：最终 agent + 默认 Profile 占位。"""
        candidates = [(agent, round(confidence, 4))]
        if agent != default_profile:
            candidates.append((default_profile, 0.0))
        return tuple(candidates)

    def _fallback(
        self,
        default_profile: str,
        reason: str,
        invalid_default: bool,
        candidates: tuple[tuple[str, float], ...],
        started: float,
        llm_used: bool,
    ) -> RouteDecision:
        """构造默认 Profile 兜底决策。

        说明:
            invalid_default 优先作为原因，便于运维发现配置错误。
        """
        final_reason = "invalid_default_profile" if invalid_default else reason
        return self._finish(
            default_profile,
            0.0,
            final_reason,
            RouteStrategy.FALLBACK,
            candidates,
            started,
            llm_used,
        )

    def _finish(
        self,
        agent: str,
        confidence: float,
        reason: str,
        strategy: RouteStrategy,
        candidates: tuple[tuple[str, float], ...],
        started: float,
        llm_used: bool,
    ) -> RouteDecision:
        """组装决策并记录 route_decided 事件。"""
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        decision = RouteDecision(
            agent=agent,
            confidence=round(confidence, 4),
            reason=reason,
            strategy=strategy,
            candidates=candidates,
            latency_ms=latency_ms,
            llm_used=llm_used,
        )
        # candidates 只记录名称与得分，最多 3 项；不包含用户消息或 LLM 原文
        log_event(
            "route_decided",
            agent=agent,
            confidence=decision.confidence,
            strategy=strategy.value,
            reason=reason,
            latency_ms=latency_ms,
            llm_used=llm_used,
            candidates=json.dumps(list(candidates[:3]), ensure_ascii=False),
        )
        return decision


def _is_timeout(exc: Exception) -> bool:
    """判断异常是否属于超时类（兼容 TimeoutError 与 SDK 自定义超时）。"""
    return isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__


def _tag_reason(tag: str) -> str:
    """把 LLM 错误标签映射为 route_decided 允许的策略原因。"""
    if tag in {"timeout", "exception"}:
        return "llm_exception"
    if tag in {"parse_error", "invalid_agent"}:
        return "parse_error"
    if tag == "no_llm":
        return "no_llm"
    # 合法但低置信（或未知标签）统一归为 below_threshold
    return "below_threshold"


_router: Router | None = None


def get_router() -> Router:
    """返回全局 Router 单例；首次调用读取策略并记录 router_policy_loaded。"""
    global _router
    if _router is None:
        policy, invalid_env = RoutePolicy.from_env()
        _router = Router(policy)
        log_event(
            "router_policy_loaded",
            strategy=policy.strategy,
            confidence_threshold=policy.confidence_threshold,
            default_profile=policy.default_profile,
            llm_enabled=policy.llm_enabled,
            policy_id=_router.policy_id,
            invalid_env=invalid_env,
        )
    return _router


def reset_router() -> None:
    """重置全局 Router（仅供测试隔离；生产代码不得调用）。"""
    global _router
    _router = None

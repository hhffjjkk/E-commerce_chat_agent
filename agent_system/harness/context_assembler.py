# agent_system/harness/context_assembler.py
# 上下文组装器：system 稳定前缀 + 历史窗口 + token 预算裁剪 + 单条 user 截断。
# 只负责“messages 如何构造”，不改变 LLM 调用方式与工具 schema。

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from agent_system.harness.context_policy import (
    ContextPolicy,
    load_policy_from_env,
)
from agent_system.obs.events import log_event

# 每条消息的固定开销（role 分隔符、格式标记等），用于近似预算
_PER_MESSAGE_OVERHEAD = 4

# 截断标记：user 超长与 system 预算截断共用同一种可读提示
_USER_TRUNCATION_MARKER = "...[已截断]"
_SYSTEM_TRUNCATION_MARKER = "..."


def _is_cjk(char: str) -> bool:
    """判断字符是否属于常用中日韩统一表意文字区间。"""
    return "\u4e00" <= char <= "\u9fff"


def estimate_tokens(text: str) -> int:
    """近似估算一段文本的 token 数。

    参数:
        text: 待估算文本（None 视为空串）。

    返回:
        估算 token 数（向上取整）。

    口径说明:
        - 中文字符按 1 字 ≈ 0.6 token（偏保守的下界估计）；
        - 其他字符（英文/数字/标点）按 1 字符 ≈ 0.25 token；
        - 混合文本按字符类型分别累加后向上取整。

    误差说明:
        不保证与真实 tokenizer 一致，误差约 ±30%；仅用于预算控制，
        真实用量以 LLM 返回的 usage 为准。
    """
    if not text:
        return 0
    total = 0.0
    for char in str(text):
        total += 0.6 if _is_cjk(char) else 0.25
    return math.ceil(total)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算一组 messages 的总 token 数。

    参数:
        messages: OpenAI messages 列表。

    返回:
        role + content + 每条固定开销（4 token）的总估算值。

    说明:
        只累加 role/content 文本，不计算 tools schema 的体积；tools 由
        `tools=` 参数单独传递，预算口径在 notes 中说明。
    """
    total = 0
    for message in messages:
        total += _PER_MESSAGE_OVERHEAD
        total += estimate_tokens(str(message.get("role", "")))
        total += estimate_tokens(str(message.get("content", "")))
    return total


@dataclass(frozen=True)
class AssembledContext:
    """一次上下文组装的只读结果。

    字段说明：
        messages: 最终发送给 LLM 的 messages。
        history_in: 输入 history 条数。
        history_out: 实际保留进 messages 的 history 条数。
        dropped: 因窗口/预算丢弃的历史条数。
        truncated_user: 单条 user 是否被截断。
        estimated_tokens: 最终 messages 的估算 token 数。
        stable_prefix_hash: system + 工具描述摘要的 sha256 前 16 位。
    """

    messages: list[dict]
    history_in: int
    history_out: int
    dropped: int
    truncated_user: bool
    estimated_tokens: int
    stable_prefix_hash: str


def _tool_sort_key(schema: dict) -> str:
    """按 function.name 排序，保证工具顺序不影响稳定前缀哈希。"""
    return str(schema.get("function", {}).get("name", ""))


class ContextAssembler:
    """按策略组装 LLM messages。

    生命周期:
        由 `get_context_assembler()` 单例持有；也可在测试中直接构造独立实例。
        内部无状态（除策略外），同一实例可被多个请求复用。
    """

    def __init__(self, policy: ContextPolicy):
        """保存并校验策略。

        参数:
            policy: 上下文策略参数。

        异常:
            ValueError: policy.validate() 不通过。
        """
        policy.validate()
        self.policy = policy
        self.policy_id = policy.policy_id()

    def _marker_message(self, dropped: int) -> dict | None:
        """构造历史压缩标记消息；不需要标记时返回 None。"""
        if dropped <= 0 or not self.policy.enable_truncation_marker:
            return None
        return {
            "role": "system",
            "content": f"[上下文已压缩：早期 {dropped} 条对话已省略]",
        }

    def _stable_prefix_hash(self, system_prompt: str, tools_schemas: list[dict] | None) -> str:
        """计算稳定前缀哈希（system + 排序后的工具 schema 摘要）。

        参数:
            system_prompt: 系统提示词，逐字参与哈希。
            tools_schemas: OpenAI tools 参数列表，可为空。

        返回:
            sha256 十六进制前 16 位。

        说明:
            该值将作为阶段五语义缓存的 Key 前缀组成部分；marker、历史、user
            都不参与，因此预算不同不会破坏缓存命中。
        """
        tools_str = ""
        if tools_schemas and self.policy.stable_prefix_mode:
            # 先按 name 排序再序列化，消除工具注册/传入顺序带来的差异
            ordered = sorted(tools_schemas, key=_tool_sort_key)
            tools_str = json.dumps(ordered, sort_keys=True, ensure_ascii=False)
        raw = system_prompt + "\n" + tools_str
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _truncate_system_to_budget(
        self,
        system_prompt: str,
        other_messages: list[dict],
        budget: int,
    ) -> str:
        """在极小预算下按需截断 system 尾部。

        参数:
            system_prompt: 原始 system 提示词。
            other_messages: 除 system 外的全部消息（marker + 历史 + user）。
            budget: 可用 token 预算。

        返回:
            截断后的 system 内容；无法满足时返回最小的 "..." 标记。

        说明:
            仅在历史已保留到 min_keep_recent_messages 仍超预算时调用；
            二分查找“最长且不超预算”的前缀，避免线性扫描。
        """
        low, high = 0, len(system_prompt)
        best = _SYSTEM_TRUNCATION_MARKER
        while low <= high:
            mid = (low + high) // 2
            candidate = system_prompt[:mid] + _SYSTEM_TRUNCATION_MARKER
            total = estimate_messages_tokens(
                [{"role": "system", "content": candidate}] + other_messages
            )
            if total <= budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def assemble(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        *,
        tools_schemas: list[dict] | None = None,
    ) -> AssembledContext:
        """按策略组装 messages。

        参数:
            system_prompt: 系统提示词（正常预算下逐字保留）。
            history: 历史消息列表，元素形如 {"role": "user", "content": "..."}。
            user_message: 当前用户输入。
            tools_schemas: 当前可用工具 schema，用于稳定前缀哈希。

        返回:
            AssembledContext，包含最终 messages 与组装统计。

        说明:
            执行顺序：user 截断 -> 稳定前缀 -> 窗口裁剪 -> token 预算裁剪 ->
            压缩标记 -> 组装 -> 结构化日志。顺序不可随意调整：先截 user
            才能准确评估预算；先算 hash 才能保证 marker/预算不影响缓存键。
        """
        # 1) 单条 user 截断：超长输入先在字符层截断，避免后续预算被单条消息吃光
        truncated_user = False
        if len(user_message) > self.policy.max_user_message_chars:
            user_message = (
                user_message[: self.policy.max_user_message_chars] + _USER_TRUNCATION_MARKER
            )
            truncated_user = True

        # 2) 稳定前缀：system 内容逐字保留；工具 schema 只参与哈希，不注入 messages
        stable_prefix_hash = self._stable_prefix_hash(system_prompt, tools_schemas)
        system_message = {"role": "system", "content": system_prompt}
        user_msg = {"role": "user", "content": user_message}

        history_in = len(history)

        # 3) 窗口裁剪：只保留最近 max_history_messages 条，保持原始先后顺序
        windowed = list(history[-self.policy.max_history_messages :])
        kept = list(windowed)

        # 4) Token 预算裁剪：从最旧的历史开始丢弃；marker 的预算也一起计算
        budget = self.policy.max_total_tokens - self.policy.reserve_completion_tokens
        while True:
            dropped_now = history_in - len(kept)
            marker = self._marker_message(dropped_now)
            trial = [system_message]
            if marker is not None:
                trial.append(marker)
            trial.extend(kept)
            trial.append(user_msg)
            tokens = estimate_messages_tokens(trial)

            # 预算内，或已经到保底条数（不能再丢），停止裁剪
            if tokens <= budget or len(kept) <= self.policy.min_keep_recent_messages:
                break
            kept.pop(0)

        dropped = history_in - len(kept)
        marker = self._marker_message(dropped)

        # 4.1) 极小预算兜底：保底历史仍超预算时，截断 system 尾部而不是继续丢历史
        merged = [system_message]
        if marker is not None:
            merged.append(marker)
        merged.extend(kept)
        merged.append(user_msg)
        if estimate_messages_tokens(merged) > budget:
            others = ([marker] if marker is not None else []) + kept + [user_msg]
            system_message = {
                "role": "system",
                "content": self._truncate_system_to_budget(system_prompt, others, budget),
            }

        # 5) 组装：system -> marker（若有）-> 保留历史 -> 当前 user。
        # marker 只影响本次 messages，不参与 stable_prefix_hash（否则预算变化会击穿缓存）
        messages = [system_message]
        if marker is not None:
            messages.append(marker)
        messages.extend(kept)
        messages.append(user_msg)

        estimated_tokens = estimate_messages_tokens(messages)

        # 6) 结构化日志：policy_id 供阶段六做策略对比评测
        log_event(
            "context_assembled",
            history_in=history_in,
            history_out=len(kept),
            dropped=dropped,
            truncated_user=truncated_user,
            estimated_tokens=estimated_tokens,
            stable_prefix_hash=stable_prefix_hash,
            policy_id=self.policy_id,
        )

        return AssembledContext(
            messages=messages,
            history_in=history_in,
            history_out=len(kept),
            dropped=dropped,
            truncated_user=truncated_user,
            estimated_tokens=estimated_tokens,
            stable_prefix_hash=stable_prefix_hash,
        )


_assembler: ContextAssembler | None = None


def get_context_assembler() -> ContextAssembler:
    """返回全局 ContextAssembler 单例。

    返回:
        ContextAssembler 实例。

    说明:
        首次调用时从环境变量加载策略，并记录一条 `context_policy_loaded` 事件；
        非法配置项会出现在 `invalid_env` 字段中，便于排查拼写/取值问题。
    """
    global _assembler
    if _assembler is None:
        policy, invalid_env = load_policy_from_env()
        _assembler = ContextAssembler(policy)
        log_event(
            "context_policy_loaded",
            policy_id=_assembler.policy_id,
            max_history_messages=policy.max_history_messages,
            max_total_tokens=policy.max_total_tokens,
            reserve_completion_tokens=policy.reserve_completion_tokens,
            min_keep_recent_messages=policy.min_keep_recent_messages,
            max_user_message_chars=policy.max_user_message_chars,
            stable_prefix_mode=policy.stable_prefix_mode,
            invalid_env=invalid_env,
        )
    return _assembler


def reset_context_assembler() -> None:
    """重置全局 Assembler（仅供测试隔离使用）。

    说明:
        生产代码不得调用：重置后下次组装会重新读取环境变量，
        可能导致同一进程内策略突变。
    """
    global _assembler
    _assembler = None

# agent_system/harness/router_prompts.py
# LLM 路由的提示词模板与严格 JSON 解析辅助。
# 路由是“元任务”：只判断业务域，不回答业务问题，因此不复用业务 SYSTEM_PROMPT。

from __future__ import annotations

import json
import re

from agent_system.harness.agent_profile import AgentProfile

ROUTER_SYSTEM_PROMPT = """你是电商客服的路由器。请根据用户消息，从以下子 Agent 中选择最合适的一个：

{profiles_block}

输出严格的 JSON，不要包含任何解释文字或代码块围栏：
{{"agent": "<profile.name>", "confidence": <0.0-1.0>, "reason": "<20 字以内的中文理由>"}}

规则：
- 只输出一个 agent；
- confidence 反映你的确定程度，不确定时给低分；
- reason 只描述判断依据，不重复用户原话。
"""


def render_profiles_block(profiles: list[AgentProfile]) -> str:
    """把 Profile 列表渲染成路由提示词中的候选块。

    参数:
        profiles: 已注册的子 Agent 列表。

    返回:
        每行 `- name: description` 的文本。
    """
    return "\n".join(f"- {profile.name}: {profile.description}" for profile in profiles)


def build_router_messages(user_message: str, profiles: list[AgentProfile]) -> list[dict]:
    """构造路由 LLM 的 messages。

    参数:
        user_message: 用户原始消息。
        profiles: 候选 Profile。

    返回:
        [{"role": "system", ...}, {"role": "user", ...}]。

    说明:
        要求严格 JSON 是为了避免解析歧义；不复用业务 SYSTEM_PROMPT，
        因为业务规则（退换货、转人工等）会干扰“选哪个域”的判断。
    """
    system = ROUTER_SYSTEM_PROMPT.format(profiles_block=render_profiles_block(profiles))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]


def _strip_code_fence(raw: str) -> str:
    """去除 Markdown 代码块围栏，保留其中的 JSON 文本。"""
    match = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw.strip()


def parse_router_response(
    raw: str,
    valid_names: set[str],
) -> tuple[str | None, float, str]:
    """解析 LLM 路由响应，返回 (agent, confidence, reason)。

    参数:
        raw: LLM 原始文本。
        valid_names: 合法 Profile name 集合。

    返回:
        解析成功且 agent 合法时返回 (agent, confidence, reason)；
        否则返回 (None, 0.0, 错误标签)，错误标签为 parse_error 或 invalid_agent。

    容错边界:
        - 先去除 ```json ... ``` 围栏；
        - 用 raw_decode 只取第一个 JSON 对象，忽略前后噪音；
        - confidence 缺失给 0.5，越界夹到 [0,1]；
        - reason 缺失给空字符串；
        - 任何解析失败都不抛异常，交给 Router 降级。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, 0.0, "parse_error"

    text = _strip_code_fence(raw)
    start = text.find("{")
    if start < 0:
        return None, 0.0, "parse_error"

    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, ValueError):
        return None, 0.0, "parse_error"

    if not isinstance(payload, dict):
        return None, 0.0, "parse_error"

    agent = payload.get("agent")
    if not isinstance(agent, str) or agent not in valid_names:
        return None, 0.0, "invalid_agent"

    confidence = payload.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    return agent, confidence, reason

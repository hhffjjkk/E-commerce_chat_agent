"""ContextAssembler 单测：窗口、预算、截断、稳定前缀、日志与 LLM 路径接入。"""

import hashlib
import re
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.agent import LLMClient, SYSTEM_PROMPT  # noqa: E402
from agent_system.harness import context_assembler as assembler_module  # noqa: E402
from agent_system.harness.context_assembler import (  # noqa: E402
    ContextAssembler,
    estimate_messages_tokens,
    estimate_tokens,
)
from agent_system.harness.context_policy import ContextPolicy  # noqa: E402

# SYSTEM_PROMPT 在阶段二/重构期间逐字未变，用哈希锁定防止误改
EXPECTED_SYSTEM_PROMPT_SHA256 = (
    "647f94fa2e2c85a4ce3528d8e64b145f8ca723082fb205ed5683685f4e52d755"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "查订单",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_product",
            "description": "查商品",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _policy(**overrides) -> ContextPolicy:
    """基于默认策略覆写字段，保证各用例策略合法。"""
    return replace(ContextPolicy(), **overrides)


def _history(count: int, content: str = "历史") -> list[dict]:
    """构造交错 user/assistant 的历史消息。"""
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"{content}{index}"}
        for index in range(count)
    ]


def test_token_estimation_profile():
    """估算口径：中文 0.6/字、其他 0.25/字符，向上取整。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2  # 2 * 0.6 -> ceil = 2
    assert estimate_tokens("abcd") == 1  # 4 * 0.25
    assert estimate_messages_tokens([{"role": "user", "content": "abcd"}]) == 1 + 4 + 1


def test_basic_assembly_keeps_system_verbatim():
    """system + 2 条历史 + user，system 内容逐字保持。"""
    assembler = ContextAssembler(_policy())
    result = assembler.assemble("SYSTEM", _history(2), "hi")

    assert len(result.messages) == 4
    assert result.messages[0] == {"role": "system", "content": "SYSTEM"}
    assert result.messages[-1] == {"role": "user", "content": "hi"}
    assert result.history_in == 2
    assert result.history_out == 2
    assert result.dropped == 0
    assert result.truncated_user is False


def test_history_window_truncation_and_marker():
    """25 条历史 + 窗口 20：保留最近 20 条并插入压缩标记。"""
    assembler = ContextAssembler(_policy(max_history_messages=20, min_keep_recent_messages=4))
    result = assembler.assemble("S", _history(25), "hi")

    assert result.history_in == 25
    assert result.history_out == 20
    assert result.dropped == 5
    assert result.messages[1]["role"] == "system"
    assert result.messages[1]["content"].startswith("[上下文已压缩：早期 5 条")
    # 保留的应是最近 20 条
    assert result.messages[2:] == _history(25)[5:] + [{"role": "user", "content": "hi"}]


def test_acceptance_thirty_history():
    """验收场景：30 条历史、窗口 20、预算 8000/1500 -> 保留 20、丢 10、不超预算。"""
    assembler = ContextAssembler(_policy())
    result = assembler.assemble("S", _history(30), "hi")

    assert result.history_out == 20
    assert result.dropped == 10
    assert result.estimated_tokens <= 8000 - 1500
    assert result.messages[1]["content"].startswith("[上下文已压缩：早期 10 条")


def test_token_budget_drops_oldest_history():
    """超预算时从最旧的历史开始丢弃，最终估算不超过预算。"""
    policy = _policy(
        max_history_messages=100,
        max_total_tokens=400,
        reserve_completion_tokens=100,
        min_keep_recent_messages=2,
    )
    assembler = ContextAssembler(policy)
    history = _history(10, content="x" * 100)
    result = assembler.assemble("S", history, "hi")

    assert result.dropped > 0
    assert result.history_out >= policy.min_keep_recent_messages
    assert result.estimated_tokens <= policy.max_total_tokens - policy.reserve_completion_tokens
    # 保留的是最近若干条，最后一条历史必须还在
    assert result.messages[-2] == history[-1]


def test_min_keep_falls_back_to_system_truncation():
    """极小预算下不继续丢历史，改为截断 system。"""
    policy = _policy(
        max_history_messages=10,
        max_total_tokens=260,
        reserve_completion_tokens=50,
        min_keep_recent_messages=2,
    )
    assembler = ContextAssembler(policy)
    result = assembler.assemble("S" * 200, _history(5, content="x" * 300), "hi")

    assert result.history_out >= policy.min_keep_recent_messages
    assert result.estimated_tokens <= policy.max_total_tokens - policy.reserve_completion_tokens
    # system 被截断：不再等于原文，且以省略号结尾
    system_content = result.messages[0]["content"]
    assert system_content != "S" * 200
    assert system_content.endswith("...")


def test_user_message_truncation():
    """单条 user 超长时尾截断并追加中文标记。"""
    assembler = ContextAssembler(_policy(max_user_message_chars=10))
    result = assembler.assemble("S", [], "abcdefghijklmnopqrst")

    assert result.truncated_user is True
    assert result.messages[-1]["content"] == "abcdefghij" + "...[已截断]"


def test_stable_prefix_hash_behaviour():
    """hash 与工具顺序无关、与 system 有关、与丢弃数量无关。"""
    assembler = ContextAssembler(_policy(max_history_messages=5))
    base = assembler.assemble("SYSTEM", [], "hi", tools_schemas=TOOLS)
    reordered = assembler.assemble("SYSTEM", [], "hi", tools_schemas=list(reversed(TOOLS)))
    changed = assembler.assemble("SYSTEM!", [], "hi", tools_schemas=TOOLS)
    dropped = assembler.assemble("SYSTEM", _history(10), "hi", tools_schemas=TOOLS)

    assert re.fullmatch(r"[0-9a-f]{16}", base.stable_prefix_hash)
    assert base.stable_prefix_hash == reordered.stable_prefix_hash
    assert base.stable_prefix_hash != changed.stable_prefix_hash
    assert base.stable_prefix_hash == dropped.stable_prefix_hash
    assert dropped.dropped == 5


def test_context_assembled_event(monkeypatch):
    """组装时恰好产生一条 context_assembled 事件，字段齐全。"""
    events = []

    def fake_log_event(event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(assembler_module, "log_event", fake_log_event)
    assembler = ContextAssembler(_policy())
    assembler.assemble("S", _history(3), "hi", tools_schemas=TOOLS)

    assembled = [e for e in events if e["event"] == "context_assembled"]
    assert len(assembled) == 1
    payload = assembled[0]
    assert payload["history_in"] == 3
    assert payload["history_out"] == 3
    assert payload["dropped"] == 0
    assert payload["truncated_user"] is False
    assert payload["estimated_tokens"] > 0
    assert re.fullmatch(r"[0-9a-f]{16}", payload["stable_prefix_hash"])
    assert re.fullmatch(r"[0-9a-f]{8}", payload["policy_id"])


def test_build_messages_uses_assembler(monkeypatch):
    """LLM 路径：_build_messages 走 Assembler，并记录 context_assembled。"""
    events = []

    def fake_log_event(event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(assembler_module, "log_event", fake_log_event)
    bot = object.__new__(LLMClient)
    messages = LLMClient._build_messages(
        bot,
        "hi",
        [{"role": "user", "content": "上一轮"}],
    )

    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[-1] == {"role": "user", "content": "hi"}
    assert bot._last_assembled is not None
    assert any(e["event"] == "context_assembled" for e in events)


def test_system_prompt_is_unchanged():
    """SYSTEM_PROMPT 逐字未变（哈希锁定）。"""
    assert hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest() == EXPECTED_SYSTEM_PROMPT_SHA256

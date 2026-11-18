"""_build_messages 的 Profile 隔离测试：system prompt 与稳定前缀哈希。"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_key, None)

from agent_system.agent import LLMClient, SYSTEM_PROMPT  # noqa: E402
from agent_system.harness.agent_profile import reset_profile_registry  # noqa: E402
from agent_system.harness.builtin_profiles import (  # noqa: E402
    AFTER_SALES_PROFILE,
    PRE_SALES_PROFILE,
    compose_system_prompt,
    register_builtin_profiles,
)
from agent_system.harness.builtin_tools import register_all  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """注册工具与 Profile。"""
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    yield
    reset_profile_registry()
    register_builtin_profiles()


def _bot():
    """构造绕过 __init__ 的 LLMClient。"""
    return object.__new__(LLMClient)


def test_profile_none_keeps_system_prompt():
    """profile=None 时首条 system 仍是原 SYSTEM_PROMPT。"""
    bot = _bot()
    messages = LLMClient._build_messages(bot, "你好", [])
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_profile_uses_composed_prompt():
    """profile 非 None 时使用 compose_system_prompt(profile)。"""
    bot = _bot()
    messages = LLMClient._build_messages(
        bot, "这个商品多少钱", [], profile=PRE_SALES_PROFILE
    )
    assert messages[0] == {
        "role": "system",
        "content": compose_system_prompt(PRE_SALES_PROFILE),
    }


def test_stable_prefix_hash_is_profile_scoped():
    """同 Profile 两次 hash 相同，不同 Profile 互异。"""
    bot = _bot()
    LLMClient._build_messages(bot, "你好", [], profile=PRE_SALES_PROFILE)
    first = bot._last_assembled.stable_prefix_hash
    LLMClient._build_messages(bot, "你好", [], profile=PRE_SALES_PROFILE)
    second = bot._last_assembled.stable_prefix_hash
    LLMClient._build_messages(bot, "你好", [], profile=AFTER_SALES_PROFILE)
    third = bot._last_assembled.stable_prefix_hash

    assert first == second
    assert first != third


def test_context_assembler_still_truncates_with_profile():
    """传入 profile 后，历史窗口/预算裁剪逻辑仍生效。"""
    bot = _bot()
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"历史{i}"}
        for i in range(30)
    ]
    LLMClient._build_messages(bot, "你好", history, profile=PRE_SALES_PROFILE)

    assembled = bot._last_assembled
    assert assembled.history_out == 20
    assert assembled.dropped == 10

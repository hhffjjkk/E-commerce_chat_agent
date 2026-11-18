"""内置 Profile 单测：工具子集、写权限、prompt 组合稳定性与注册幂等。"""

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.agent import SYSTEM_PROMPT  # noqa: E402
from agent_system.harness import builtin_profiles as profiles_module  # noqa: E402
from agent_system.harness.agent_profile import (  # noqa: E402
    ProfileRegistry,
    get_profile_registry,
    reset_profile_registry,
)
from agent_system.harness import agent_profile as profile_module  # noqa: E402
from agent_system.harness.builtin_profiles import (  # noqa: E402
    AFTER_SALES_PROFILE,
    ORDER_PROFILE,
    PRE_SALES_PROFILE,
    compose_system_prompt,
    register_builtin_profiles,
)
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.tool_registry import ToolRegistry, get_registry  # noqa: E402


@pytest.fixture(autouse=True)
def profiles_ready():
    """先注册工具，再重建并注册内置 Profile。"""
    register_all()
    reset_profile_registry()
    register_builtin_profiles()
    yield
    reset_profile_registry()
    # 测试结束后恢复内置 Profile，避免后续模块的 /chat 因空注册表而 500
    register_builtin_profiles()


def test_builtin_profiles_are_registered_in_order():
    """三个内置 Profile 按固定顺序注册。"""
    assert get_profile_registry().names() == ["pre_sales", "order", "after_sales"]


def test_tool_subsets_match_spec():
    """工具子集与 allowed_write_tools 逐项符合设计表。"""
    assert PRE_SALES_PROFILE.tool_names == ("query_product", "search_faq")
    assert ORDER_PROFILE.tool_names == ("query_order", "query_logistics")
    assert AFTER_SALES_PROFILE.tool_names == (
        "query_order",
        "apply_refund",
        "cancel_order",
        "confirm_action",
        "reject_action",
    )

    assert PRE_SALES_PROFILE.allowed_write_tools == ()
    assert ORDER_PROFILE.allowed_write_tools == ()
    assert AFTER_SALES_PROFILE.allowed_write_tools == ("apply_refund", "cancel_order")


def test_all_tools_exist_in_registry():
    """Profile 引用的工具必须全部已注册。"""
    registered = get_registry().snapshot()
    for profile in (PRE_SALES_PROFILE, ORDER_PROFILE, AFTER_SALES_PROFILE):
        assert set(profile.tool_names) <= set(registered)


def test_write_tools_have_write_tag():
    """写工具带 write 标签，供 Profile 校验与路由器消费。"""
    registry = get_registry()
    for name in ("apply_refund", "cancel_order"):
        assert "write" in registry.get(name).tags


def test_compose_prompt_prefix_and_content():
    """组合 prompt 以 SYSTEM_PROMPT 开头，包含标题与专属关键句。"""
    prompt = compose_system_prompt(PRE_SALES_PROFILE)

    assert prompt.startswith(SYSTEM_PROMPT)
    assert "## 当前子 Agent：售前咨询" in prompt
    assert "优先用 query_product" in prompt
    # 同一 Profile 两次调用逐字一致
    assert prompt == compose_system_prompt(PRE_SALES_PROFILE)


def test_compose_prompt_hashes_are_stable_and_distinct():
    """同 Profile hash 稳定，不同 Profile hash 不同，base 变化则 hash 变化。"""
    hashes = {
        name: hashlib.sha256(compose_system_prompt(profile).encode("utf-8")).hexdigest()
        for name, profile in (
            ("pre_sales", PRE_SALES_PROFILE),
            ("order", ORDER_PROFILE),
            ("after_sales", AFTER_SALES_PROFILE),
        )
    }
    assert len(set(hashes.values())) == 3
    assert hashes["pre_sales"] == hashlib.sha256(
        compose_system_prompt(PRE_SALES_PROFILE).encode("utf-8")
    ).hexdigest()

    # monkeypatch base provider，验证共享规则变化会改变组合结果
    original = profiles_module.BASE_RULES_PROVIDER
    try:
        profiles_module.BASE_RULES_PROVIDER = lambda: "新的共享规则"
        changed = hashlib.sha256(
            compose_system_prompt(PRE_SALES_PROFILE).encode("utf-8")
        ).hexdigest()
    finally:
        profiles_module.BASE_RULES_PROVIDER = original
    assert changed != hashes["pre_sales"]


def test_register_is_idempotent():
    """重复注册不抛异常，名称与内容保持一致。"""
    before = get_profile_registry().snapshot()
    register_builtin_profiles()
    after = get_profile_registry().snapshot()
    assert before == after


def test_empty_tool_registry_gives_actionable_error(monkeypatch):
    """Tool Registry 为空时提示先调用 register_all。"""
    empty = ToolRegistry()
    monkeypatch.setattr(profile_module, "get_registry", lambda: empty)

    with pytest.raises(ValueError) as exc_info:
        register_builtin_profiles(ProfileRegistry())
    assert "register_all" in str(exc_info.value)

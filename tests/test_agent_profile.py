"""AgentProfile 与 ProfileRegistry 单测：校验、注册、过滤、单例。"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.harness.agent_profile import (  # noqa: E402
    AgentProfile,
    ProfileRegistry,
    get_profile_registry,
    reset_profile_registry,
)
from agent_system.harness.builtin_profiles import register_builtin_profiles  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402


def _profile(**overrides) -> AgentProfile:
    """构造合法 Profile，便于每个用例按需覆写。"""
    params = {
        "name": "test_agent",
        "display_name": "测试",
        "description": "测试用子 Agent",
        "system_prompt": "你是测试助手。",
        "tool_names": ("query_order",),
        "allowed_write_tools": (),
        "tags": ("business",),
    }
    params.update(overrides)
    return AgentProfile(**params)


@pytest.fixture(autouse=True)
def tools_ready():
    """确保 Tool Registry 已注册（conftest 也会做，这里显式保证）。"""
    register_all()


def test_valid_profile_passes():
    """合法 Profile 通过校验。"""
    _profile().validate()


def test_invalid_name_is_rejected():
    """名称必须是小写字母开头的标识符。"""
    with pytest.raises(ValueError):
        _profile(name="Test-Agent").validate()
    with pytest.raises(ValueError):
        _profile(name="1agent").validate()


def test_unregistered_tool_is_rejected():
    """引用未注册工具时明确报错。"""
    with pytest.raises(ValueError):
        _profile(tool_names=("query_order", "not_registered")).validate()


def test_write_tools_must_be_subset_of_tool_names():
    """allowed_write_tools 必须是 tool_names 的子集。"""
    with pytest.raises(ValueError):
        _profile(
            tool_names=("query_order",),
            allowed_write_tools=("apply_refund",),
        ).validate()


def test_write_tools_require_write_tag():
    """写权限工具必须在 Registry 中带 write 标签。"""
    with pytest.raises(ValueError):
        _profile(
            tool_names=("query_order",),
            allowed_write_tools=("query_order",),
        ).validate()


def test_system_prompt_cannot_be_empty():
    """专属 prompt 不能为空。"""
    with pytest.raises(ValueError):
        _profile(system_prompt="  ").validate()


def test_registry_crud_and_conflict():
    """注册、查询、列表、冲突覆盖与快照隔离。"""
    registry = ProfileRegistry()
    profile = _profile()
    registry.register(profile)

    assert registry.get("test_agent") is profile
    assert registry.get("missing") is None
    assert registry.names() == ["test_agent"]
    assert registry.list() == [profile]
    assert registry.list(tags=("business",)) == [profile]
    assert registry.list(tags=("other",)) == []

    with pytest.raises(ValueError):
        registry.register(replace(profile, description="另一份定义"))

    registry.register(replace(profile, description="覆盖后的定义"), override=True)
    assert registry.get("test_agent").description == "覆盖后的定义"

    snapshot = registry.snapshot()
    snapshot.pop("test_agent")
    assert registry.get("test_agent") is not None


def test_singleton_and_reset():
    """单例稳定，reset 后为空；结束时恢复内置 Profile 避免污染。"""
    reset_profile_registry()
    try:
        first = get_profile_registry()
        assert get_profile_registry() is first
        assert first.names() == []

        reset_profile_registry()
        assert get_profile_registry() is not first
    finally:
        reset_profile_registry()
        register_builtin_profiles()
    assert get_profile_registry().names() == ["pre_sales", "order", "after_sales"]

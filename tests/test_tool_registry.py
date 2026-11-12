"""Tool Registry 行为单测：注册、冲突、启停、过滤、schema 生成与调用。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.errors.exceptions import ToolExecutionError  # noqa: E402
from agent_system.harness.builtin_tools import register_all  # noqa: E402
from agent_system.harness.tool_registry import (  # noqa: E402
    ToolRegistry,
    ToolSpec,
    get_registry,
    reset_registry,
)


def _spec(
    name: str = "demo_tool",
    description: str = "演示工具",
    parameters: dict | None = None,
    func=None,
    *,
    enabled: bool = True,
    tags: tuple[str, ...] = ("demo",),
) -> ToolSpec:
    """构造一个合法的 ToolSpec，便于各用例复用。"""
    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters
        or {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        callable=func or (lambda value: {"echo": value}),
        enabled=enabled,
        tags=tags,
    )


@pytest.fixture
def registry():
    """每个用例使用独立注册表，避免相互污染。"""
    return ToolRegistry()


def test_register_get_and_list(registry):
    """注册后可按名称查询，并出现在启用列表中。"""
    spec = _spec()
    registry.register(spec)

    assert registry.get("demo_tool") is spec
    assert registry.get("missing") is None
    assert registry.list(enabled_only=True) == [spec]


def test_register_conflict_and_override(registry):
    """默认冲突抛 ValueError；override=True 覆盖并保留最新 spec。"""
    registry.register(_spec(description="第一版"))
    with pytest.raises(ValueError):
        registry.register(_spec(description="第二版"))

    registry.register(_spec(description="第三版"), override=True)
    assert registry.get("demo_tool").description == "第三版"
    assert registry.snapshot()["demo_tool"].description == "第三版"


def test_disable_and_enable_affect_schemas_and_call(registry):
    """禁用后 schemas 不含该工具、call 抛领域异常；重新启用后恢复。"""
    registry.register(_spec(func=lambda value: {"ok": value}))
    registry.disable("demo_tool")

    assert registry.schemas() == []
    with pytest.raises(ToolExecutionError) as exc_info:
        registry.call("demo_tool", value="x")
    assert exc_info.value.code == "tool_failed"
    assert exc_info.value.details["tool"] == "demo_tool"

    registry.enable("demo_tool")
    assert [item["function"]["name"] for item in registry.schemas()] == ["demo_tool"]
    assert registry.call("demo_tool", value="x") == {"ok": "x"}


def test_enable_and_disable_missing_tool_raise_keyerror(registry):
    """启停属于显式操作，未注册名称应抛 KeyError。"""
    with pytest.raises(KeyError):
        registry.enable("missing")
    with pytest.raises(KeyError):
        registry.disable("missing")


def test_schemas_filter_by_names(registry):
    """schemas(names=...) 按给定顺序返回，并拒绝未注册/已禁用名称。"""
    registry.register(_spec(name="tool_b"))
    registry.register(_spec(name="tool_a"))

    schemas = registry.schemas(names=["tool_a", "tool_b"])
    assert [item["function"]["name"] for item in schemas] == ["tool_a", "tool_b"]

    with pytest.raises(ValueError):
        registry.schemas(names=["missing"])

    registry.disable("tool_a")
    with pytest.raises(ValueError):
        registry.schemas(names=["tool_a"])


def test_list_filter_by_tags(registry):
    """标签过滤采用“至少命中一个”的语义。"""
    registry.register(_spec(name="order_tool", tags=("builtin", "order")))
    registry.register(_spec(name="faq_tool", tags=("builtin", "faq")))

    assert [s.name for s in registry.list(tags=("order",))] == ["order_tool"]
    assert {s.name for s in registry.list(tags=("order", "faq"))} == {
        "order_tool",
        "faq_tool",
    }
    assert registry.list(tags=None) and len(registry.list(tags=None)) == 2


def test_invalid_specs_are_rejected(registry):
    """名称非法、parameters 结构错误、callable 为空都必须抛 ValueError。"""
    with pytest.raises(ValueError):
        registry.register(_spec(name="bad-name"))

    with pytest.raises(ValueError):
        registry.register(
            _spec(parameters={"properties": {"x": {"type": "string"}}})
        )

    with pytest.raises(ValueError):
        registry.register(_spec(parameters={"type": "object"}))

    with pytest.raises(ValueError):
        registry.register(
            ToolSpec(
                name="no_impl",
                description="只有 schema 没有实现",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}},
                callable=None,
            )
        )


def test_snapshot_is_a_shallow_copy(registry):
    """snapshot 返回新 dict，外部增删键不影响注册表内部状态。"""
    registry.register(_spec())
    snapshot = registry.snapshot()
    snapshot.pop("demo_tool")

    assert registry.get("demo_tool") is not None
    # 新的 snapshot 仍然包含该工具，证明上面对返回值的删除没有影响内部状态
    assert "demo_tool" in registry.snapshot()


def test_schemas_returns_independent_parameters(registry):
    """schemas 返回深拷贝，调用方修改不会污染注册表。"""
    registry.register(_spec())
    schemas = registry.schemas()
    schemas[0]["function"]["parameters"]["properties"]["injected"] = {"type": "string"}

    assert "injected" not in registry.get("demo_tool").parameters["properties"]


def test_builtin_register_all_is_idempotent(registry):
    """内置工具重复注册应幂等，快照保持一致。"""
    register_all(registry)
    first = registry.snapshot()
    register_all(registry)
    second = registry.snapshot()

    assert first == second
    assert len(second) == 6
    assert all(spec.source == "builtin" for spec in second.values())


def test_reset_registry_clears_singleton():
    """reset_registry 清空全局单例；结束时恢复内置工具，避免影响其他用例。"""
    reset_registry()
    try:
        assert get_registry().snapshot() == {}
    finally:
        register_all()
    assert len(get_registry().schemas()) == 6

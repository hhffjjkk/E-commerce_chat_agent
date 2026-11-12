"""内置工具注册：把 tools.py 的实现与 schema 装入 Tool Registry。

注册时机：
- `app.py` 在创建 FastAPI 实例前调用一次；
- `demo.py` 与 `tests/conftest.py` 在模块导入时调用，保证独立运行也有工具可用；
- `register_all()` 幂等，重复调用（例如测试多次导入 app）不会产生副作用。
"""

from agent_system import tools
from agent_system.harness.tool_registry import ToolRegistry, ToolSpec, get_registry

# 阶段四预埋：按业务域打标签，供“给某个 Agent 分配工具子集”时过滤使用。
# 本步骤不消费这些标签，只保证元数据完整。
_TOOL_TAGS: dict[str, tuple[str, ...]] = {
    "query_product": ("builtin", "product"),
    "query_order": ("builtin", "order"),
    "query_logistics": ("builtin", "order"),
    "search_faq": ("builtin", "faq"),
    "apply_refund": ("builtin", "after_sales"),
    "cancel_order": ("builtin", "after_sales"),
}


def register_all(registry: ToolRegistry | None = None) -> None:
    """把 `tools.TOOL_SCHEMAS` 中的工具全部注册到指定注册表。

    参数:
        registry: 目标注册表；None 时使用全局单例 `get_registry()`。

    返回:
        无。

    异常:
        ValueError: 已存在同名但定义不同的工具（说明内置定义被并发修改）。

    说明:
        幂等的原因：`app.py` 与测试可能在同一个进程里被多次导入/初始化；
        若已注册且 spec 完全一致则直接跳过，避免 "工具已存在" 报错。
    """
    target = registry if registry is not None else get_registry()

    for name, schema in tools.TOOL_SCHEMAS.items():
        func = getattr(tools, name)  # 使用原函数，避免再包一层导致 _tool_guard 执行两次
        spec = ToolSpec(
            name=name,
            description=schema["description"],
            parameters=schema["parameters"],
            callable=func,
            enabled=True,
            tags=_TOOL_TAGS.get(name, ("builtin",)),
            source="builtin",
        )
        existing = target.get(name)
        if existing is not None:
            if existing == spec:
                # 完全一致：重复注册属于正常幂等调用，直接跳过
                continue
            # 同名但定义不同：视为配置漂移，必须显式报错而不是静默覆盖
            raise ValueError(
                f"builtin 工具定义被并发修改: {name}（请检查是否重复注册了不同实现）"
            )
        target.register(spec)

"""Harness 工具注册表：统一管理工具的 schema、实现、启停与标签。

设计动机：
- 重构前工具 schema 硬编码在 `LLMClient.TOOLS`，名称到实现的映射又分散在 `_call_tool`，
  两处需要人工保持同步；
- 注册表把「声明」和「执行」收敛到一处，后续多 Agent（阶段四）可按标签/名称取子集，
  也能在不改代码的情况下启停工具；
- 本模块只管理元数据与调用分发，不改变任何工具自身的 schema 与行为。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from agent_system.errors.exceptions import ToolExecutionError
from agent_system.obs.logging_config import get_logger

# OpenAI function name 约束：字母/下划线开头，后续允许数字
_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _warn(event: str, **fields) -> None:
    """输出一条结构化 WARNING 日志（用于覆盖注册等需要告警的场景）。"""
    logger = get_logger()
    try:
        logger.warning(event, extra={"event": event, **fields})
    except Exception:  # noqa: BLE001 - 日志失败不能影响注册流程
        pass


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的声明式描述。

    字段说明：
        name: OpenAI tools 中的 function.name。
        description: 给模型看的用途描述。
        parameters: JSON Schema（必须为 object 类型且含 properties）。
        callable: 实际执行函数，签名需与 tools.* 一致。
        enabled: 是否参与 schemas() 与 call()。
        tags: 标签元组，供阶段四按 Agent 分配工具子集（本步骤不消费）。
        source: 来源标记，便于诊断（内置工具固定为 builtin）。
    """

    name: str
    description: str
    parameters: dict
    callable: Callable[..., Any]
    enabled: bool = True
    tags: tuple[str, ...] = ()
    source: str = "builtin"


def _validate_spec(spec: ToolSpec) -> None:
    """校验 ToolSpec 是否满足注册约束。

    参数:
        spec: 待注册的工具声明。

    异常:
        ValueError: 名称非法、缺少可调用实现、parameters 结构不合法。
    """
    if not isinstance(spec.name, str) or not _NAME_PATTERN.fullmatch(spec.name):
        raise ValueError(f"工具名不合法（需匹配 ^[a-zA-Z_][a-zA-Z0-9_]*$）: {spec.name!r}")
    # 本步骤只接受「有实现」的工具；只有 schema 的配置视为错误
    if spec.callable is None or not callable(spec.callable):
        raise ValueError(f"工具 {spec.name} 缺少可调用实现")
    parameters = spec.parameters
    if not isinstance(parameters, dict):
        raise ValueError(f"工具 {spec.name} 的 parameters 必须是 dict")
    if parameters.get("type") != "object":
        raise ValueError(f"工具 {spec.name} 的 parameters.type 必须为 'object'")
    if not isinstance(parameters.get("properties"), dict):
        raise ValueError(f"工具 {spec.name} 的 parameters.properties 必须存在且为 dict")


class ToolRegistry:
    """工具注册表：注册、启停、过滤、生成 OpenAI tools 参数并分发调用。

    生命周期：
        默认由模块级单例 `get_registry()` 持有；测试可创建独立实例或用
        `reset_registry()` 隔离。实例内部只做同步字典操作，无线程/异步安全假设。
    """

    def __init__(self) -> None:
        """初始化空注册表；内置工具由 `builtin_tools.register_all()` 装入。"""
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec, *, override: bool = False) -> None:
        """注册一个工具。

        参数:
            spec: 工具声明。
            override: 为 True 时覆盖同名工具并记录 WARNING；否则冲突抛错。

        异常:
            ValueError: spec 非法或名称冲突且未允许覆盖。
        """
        _validate_spec(spec)
        existing = self._tools.get(spec.name)
        if existing is not None and not override:
            # 冲突默认失败：避免两个模块静默抢注同名工具导致行为漂移
            raise ValueError(f"工具已存在: {spec.name}（如需覆盖请传 override=True）")
        if existing is not None and override:
            # 覆盖属于显式行为，必须留下告警，便于排查“谁改了工具定义”
            _warn("tool_registry_override", tool=spec.name, source=spec.source)
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        """注销工具；不存在时静默返回（便于测试与降级清理）。"""
        self._tools.pop(name, None)

    def enable(self, name: str) -> None:
        """启用工具；不存在时抛 KeyError（显式操作应暴露问题）。"""
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"工具未注册: {name}")
        self._tools[name] = ToolSpec(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            callable=spec.callable,
            enabled=True,
            tags=spec.tags,
            source=spec.source,
        )

    def disable(self, name: str) -> None:
        """禁用工具；不存在时抛 KeyError。"""
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"工具未注册: {name}")
        self._tools[name] = ToolSpec(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            callable=spec.callable,
            enabled=False,
            tags=spec.tags,
            source=spec.source,
        )

    def get(self, name: str) -> ToolSpec | None:
        """按名称查询工具（不区分 enabled）；不存在返回 None。"""
        return self._tools.get(name)

    def list(
        self,
        *,
        enabled_only: bool = False,
        tags: tuple[str, ...] | None = None,
    ) -> list[ToolSpec]:
        """列出工具。

        参数:
            enabled_only: 为 True 时只返回已启用工具。
            tags: None 表示不过滤；非空时要求工具至少命中一个标签。

        返回:
            按注册顺序排列的 ToolSpec 列表。
        """
        result = []
        for spec in self._tools.values():
            if enabled_only and not spec.enabled:
                continue
            # 标签过滤采用“至少命中一个”的语义，方便阶段四按业务域取子集
            if tags and not set(tags) & set(spec.tags):
                continue
            result.append(spec)
        return result

    def schemas(self, *, names: Iterable[str] | None = None) -> list[dict]:
        """生成 OpenAI tools 参数。

        参数:
            names: None 表示返回全部启用工具；非空时按给定顺序筛选。

        返回:
            可直接传给 `chat.completions.create(tools=...)` 的列表。

        异常:
            ValueError: names 中出现未注册或已禁用的工具。
        """
        if names is None:
            specs = [spec for spec in self._tools.values() if spec.enabled]
        else:
            specs = []
            for name in names:
                spec = self._tools.get(name)
                if spec is None:
                    # 显式指定名称却不存在，说明调用方配置有问题，直接暴露
                    raise ValueError(f"工具未注册: {name}")
                if not spec.enabled:
                    raise ValueError(f"工具已禁用: {name}")
                specs.append(spec)

        # 深拷贝 parameters，避免调用方修改返回值时污染注册表内部数据
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": copy.deepcopy(spec.parameters),
                },
            }
            for spec in specs
        ]

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """执行工具。

        参数:
            tool_name: 已注册的工具名。
            arguments: 工具入参字典；None 视为无参调用。

        返回:
            工具实现的原始返回值（通常是 dict）。

        异常:
            ToolExecutionError: 工具不存在或已禁用；工具自身的异常由 `_tool_guard` 转换。

        说明:
            tool_call 日志由 tools.py 的 `_tool_guard` 负责，这里不再重复埋点。

        设计说明（阶段三遗留 bug 修复）:
            旧签名是 `call(name, **kwargs)`，当工具入参本身也叫 `name`（例如
            `query_product(name=...)`）时，Python 会把该关键字参数绑定到 Registry
            的形参 `name` 上，于是抛出 “got multiple values for argument 'name'”。
            改为 `(tool_name, arguments)` 后，工具入参只存在于 arguments 字典中，
            Registry 形参与工具入参名彻底解耦，无论工具参数叫什么都不会冲突；
            这也与 OpenAI tools API 的 `function.arguments`（JSON → 字典）语义一致，
            未来做调用审计或参数脱敏时只需在 arguments 层统一拦截。
        """
        spec = self._tools.get(tool_name)
        if spec is None:
            # 未注册工具按工具执行失败处理，保持与 _tool_guard 一致的异常口径
            raise ToolExecutionError(
                "工具未注册",
                tool=tool_name,
                args_keys=sorted(arguments or {}),
            )
        if not spec.enabled:
            # disabled 与“未注册”区分文案，但同样归为工具执行失败
            raise ToolExecutionError(
                "工具未启用",
                tool=tool_name,
                args_keys=sorted(arguments or {}),
            )
        # arguments 为 None 时按无参调用；8 个内置工具均可用 **arguments 展开
        return spec.callable(**(arguments or {}))

    def snapshot(self) -> dict[str, ToolSpec]:
        """返回注册表的浅拷贝，便于测试与诊断。

        返回:
            新的 dict；新增/删除键不会影响内部状态，ToolSpec 本身为 frozen 不可变。
        """
        return dict(self._tools)


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """返回全局工具注册表单例；首次调用时创建空表。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_registry() -> None:
    """重置全局注册表（仅供测试隔离使用）。

    说明:
        生产代码不得调用：清空后需要重新 `register_all()`，否则工具调用会失败。
    """
    global _registry
    _registry = None

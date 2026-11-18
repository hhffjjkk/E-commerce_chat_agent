# agent_system/harness/agent_profile.py
# AgentProfile：子 Agent 的静态声明（路由依据、专属 prompt、工具子集与写权限）。
# 本步骤只提供数据结构与隔离校验，不接入路由；Router 与实际切换在阶段四后续步骤实现。

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_system.harness.tool_registry import get_registry
from agent_system.obs.logging_config import get_logger

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class AgentProfile:
    """一个子 Agent 的静态定义。

    字段说明：
        name: 稳定标识（小写字母/数字/下划线），如 pre_sales / order / after_sales。
        display_name: 中文展示名，写入组合 prompt 的小标题。
        description: 给 Router 的路由依据，必须能与其他 Profile 区分。
        system_prompt: 专属规则片段；与共享 SYSTEM_PROMPT 组合后使用。
        tool_names: 该 Agent 可用的工具子集；必须全部已在 Tool Registry 注册。
        allowed_write_tools: 允许触发的写工具子集，必须是 tool_names 子集，
            且对应工具必须带 "write" 标签；仅作为路由层的“意图声明”，
            真正执行仍由 Guardrails 两阶段确认拦截。
        tags: 归类标签（如 business），供管理端/路由器过滤。
        model_override: 可选模型覆盖；本步骤只保留字段，暂不消费。
    """

    name: str
    display_name: str
    description: str
    system_prompt: str
    tool_names: tuple[str, ...]
    allowed_write_tools: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    model_override: str | None = None

    def validate(self) -> None:
        """校验 Profile 是否自洽且与 Tool Registry 一致。

        异常:
            ValueError: 名称非法、工具未注册、写权限越界或缺少 write 标签、
                system_prompt 为空。

        说明:
            写权限校验依赖 Registry 的 `"write"` 标签；工具注册必须先于 Profile 注册，
            否则这里会明确提示注册顺序。
        """
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(f"Profile 名称不合法（需匹配 ^[a-z][a-z0-9_]*$）: {self.name!r}")
        if not self.tool_names:
            raise ValueError(f"Profile {self.name} 的 tool_names 不能为空")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError(f"Profile {self.name} 的 tool_names 存在重复项")
        if not str(self.system_prompt or "").strip():
            raise ValueError(f"Profile {self.name} 的 system_prompt 不能为空")

        registered = get_registry().snapshot()
        if not registered:
            raise ValueError("Tool Registry 为空，请先调用 builtin_tools.register_all()")

        for tool_name in self.tool_names:
            if tool_name not in registered:
                raise ValueError(f"Profile {self.name} 引用了未注册工具: {tool_name}")

        # 写权限必须是工具子集的子集：避免 Profile 声明了根本拿不到的工具
        for tool_name in self.allowed_write_tools:
            if tool_name not in self.tool_names:
                raise ValueError(
                    f"Profile {self.name} 的 allowed_write_tools 包含非 tool_names 工具: {tool_name}"
                )
            spec = registered[tool_name]
            if "write" not in spec.tags:
                raise ValueError(
                    f"Profile {self.name} 将 {tool_name} 声明为写工具，但 Registry 缺少 'write' 标签"
                )


class ProfileRegistry:
    """子 Agent Profile 注册表（只增删/查询，不提供启停）。

    为什么不做启停：
        Profile 数量极少且属于设计期决策；运行期是否可选由 Router 策略控制，
        在注册表层做启停会让“配置错误”和“策略选择”混在一起。

    生命周期:
        默认由 `get_profile_registry()` 单例持有；测试可构造独立实例。
    """

    def __init__(self) -> None:
        """初始化空注册表；内置 Profile 由 register_builtin_profiles() 装入。"""
        self._profiles: dict[str, AgentProfile] = {}

    def register(self, profile: AgentProfile, *, override: bool = False) -> None:
        """注册 Profile。

        参数:
            profile: 待注册声明。
            override: 冲突时是否覆盖；默认 False 抛 ValueError。

        异常:
            ValueError: 校验失败或名称冲突。
        """
        profile.validate()
        existing = self._profiles.get(profile.name)
        if existing is not None and not override:
            raise ValueError(
                f"Profile 已存在: {profile.name}（如需覆盖请传 override=True）"
            )
        if existing is not None and override:
            # 覆盖属于显式行为，留下结构化 WARNING 便于诊断
            try:
                get_logger().warning(
                    "profile_registry_override",
                    extra={"event": "profile_registry_override", "profile": profile.name},
                )
            except Exception:  # noqa: BLE001 - 日志失败不影响注册
                pass
        self._profiles[profile.name] = profile

    def get(self, name: str) -> AgentProfile | None:
        """按名称查询 Profile；不存在返回 None。"""
        return self._profiles.get(name)

    def list(self, *, tags: tuple[str, ...] | None = None) -> list[AgentProfile]:
        """列出 Profile；tags 非空时要求至少命中一个标签。"""
        result = []
        for profile in self._profiles.values():
            if tags and not set(tags) & set(profile.tags):
                continue
            result.append(profile)
        return result

    def names(self) -> list[str]:
        """返回注册顺序的名称列表。"""
        return list(self._profiles)

    def snapshot(self) -> dict[str, AgentProfile]:
        """返回浅拷贝，外部增删键不影响内部状态。"""
        return dict(self._profiles)


_registry: ProfileRegistry | None = None


def get_profile_registry() -> ProfileRegistry:
    """返回全局 ProfileRegistry 单例（不自动注册内置 Profile）。"""
    global _registry
    if _registry is None:
        _registry = ProfileRegistry()
    return _registry


def reset_profile_registry() -> None:
    """重置全局 ProfileRegistry（仅供测试隔离；生产代码不得调用）。"""
    global _registry
    _registry = None

"""评测任务的数据结构：TaskCase / ExpectSpec 与 JSONL 读取。

设计说明:
    - 任务集是“固定输入 + 可自动判定的期望”，不依赖外部状态；
    - 本模块只做解析与校验，不执行任务、不访问网络；
    - 所有字段可选或带默认值，保证旧的 `{"input": ...}` 形态不会被静默误用
      （缺少 id/messages 时直接抛 ValueError，避免“看似跑了其实没判定”）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 分类约定：与 docs/eval/phase6-step-1-notes.md 的分类表一一对应。
# 放在这里而不是分散在 runner，便于测试直接断言“任务集覆盖了全部分类”。
VALID_CATEGORIES: tuple[str, ...] = (
    "route_rule_hit",
    "route_llm_fallback",
    "pending_flow",
    "cache_hit",
    "cache_miss_not_cached",
    "error_path",
    "boundary_input",
    "tool_subset",
)

# 允许的注入类型：仅评测层使用，不改变业务代码。
VALID_INJECTIONS: tuple[str, ...] = (
    "tool_error",
    "llm_error",
    "route_exception",
    "confirmation_ttl",
)


@dataclass(frozen=True)
class ExpectSpec:
    """单条任务的期望；未设的字段不参与判定。

    字段:
        route_agent: 期望的 Profile name；None 表示不校验路由。
        contains: 回复必须包含的关键词列表（全部包含才通过）；空表示不校验。
        no_write_tools: True 表示本次请求不得触发任何带 write 标签的工具；
            False 表示该任务预期会调用写工具，不参与判定。
        cache_hit: True/False/None；None 表示不校验缓存命中。
        errors_allowed: 允许出现的错误码（默认空 = 不允许错误）。
    """

    route_agent: str | None = None
    contains: tuple[str, ...] = ()
    no_write_tools: bool = True
    cache_hit: bool | None = None
    errors_allowed: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict | None) -> "ExpectSpec":
        """从 JSON 字典构造期望；非法类型抛 ValueError。

        参数:
            raw: `expect` 字段的内容；None 视为空期望。

        返回:
            ExpectSpec 实例。

        异常:
            ValueError: 字段类型不符合约定（例如 contains 不是数组）。
        """
        data = dict(raw or {})
        route_agent = data.get("route_agent")
        if route_agent is not None and not isinstance(route_agent, str):
            raise ValueError("expect.route_agent 必须是字符串或 null")

        contains = data.get("contains", [])
        if not isinstance(contains, (list, tuple)) or any(
            not isinstance(item, str) for item in contains
        ):
            raise ValueError("expect.contains 必须是字符串数组")

        no_write_tools = data.get("no_write_tools", True)
        if not isinstance(no_write_tools, bool):
            raise ValueError("expect.no_write_tools 必须是布尔值")

        cache_hit = data.get("cache_hit")
        if cache_hit is not None and not isinstance(cache_hit, bool):
            raise ValueError("expect.cache_hit 必须是布尔值或 null")

        errors_allowed = data.get("errors_allowed", [])
        if not isinstance(errors_allowed, (list, tuple)) or any(
            not isinstance(item, str) for item in errors_allowed
        ):
            raise ValueError("expect.errors_allowed 必须是字符串数组")

        return cls(
            route_agent=route_agent,
            contains=tuple(contains),
            no_write_tools=no_write_tools,
            cache_hit=cache_hit,
            errors_allowed=tuple(errors_allowed),
        )


@dataclass(frozen=True)
class TaskCase:
    """一条评测任务。

    字段:
        id: 稳定标识，如 "presales-price-01"；同一任务集内不得重复。
        category: 分类，用于分组统计；取值见 VALID_CATEGORIES。
        session_id: 会话 ID；多轮任务共享同一值。
        messages: 用户消息序列；单轮为 1 条，pending 场景为 2 条。
        expect: 期望规格；多轮任务按最后一次请求判定。
        only_real_llm: 是否只在真实 LLM 模式下运行。
        notes: 测试意图，供报告展示。
        profile_hint: 期望 Profile 的提示，仅用于人读；判定以 expect.route_agent 为准。
        inject: 评测层故障注入配置；None 表示不注入。
    """

    id: str
    category: str
    session_id: str
    messages: tuple[str, ...]
    expect: ExpectSpec = field(default_factory=ExpectSpec)
    only_real_llm: bool = False
    notes: str = ""
    profile_hint: str | None = None
    inject: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "TaskCase":
        """从 JSONL 单行构造任务，并做最小必要校验。

        参数:
            raw: 反序列化后的任务字典。

        返回:
            TaskCase 实例。

        异常:
            ValueError: 缺少 id/messages、messages 为空、分类非法或注入类型未知。
        """
        if not isinstance(raw, dict):
            raise ValueError("任务必须是 JSON 对象")

        task_id = raw.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("任务缺少非空 id 字段")

        messages = raw.get("messages")
        if not isinstance(messages, (list, tuple)) or not messages:
            raise ValueError(f"任务 {task_id} 的 messages 必须是非空数组")
        if any(not isinstance(item, str) for item in messages):
            raise ValueError(f"任务 {task_id} 的 messages 只能包含字符串")

        category = raw.get("category", "")
        if category not in VALID_CATEGORIES:
            raise ValueError(
                f"任务 {task_id} 的 category 非法: {category!r}；"
                f"可选值: {', '.join(VALID_CATEGORIES)}"
            )

        only_real_llm = raw.get("only_real_llm", False)
        if not isinstance(only_real_llm, bool):
            raise ValueError(f"任务 {task_id} 的 only_real_llm 必须是布尔值")

        inject = raw.get("inject")
        if inject is not None:
            if not isinstance(inject, dict):
                raise ValueError(f"任务 {task_id} 的 inject 必须是对象")
            inject_type = inject.get("type")
            if inject_type not in VALID_INJECTIONS:
                raise ValueError(
                    f"任务 {task_id} 的注入类型未知: {inject_type!r}；"
                    f"可选值: {', '.join(VALID_INJECTIONS)}"
                )

        session_id = raw.get("session_id") or f"eval-{task_id}"
        if not isinstance(session_id, str):
            raise ValueError(f"任务 {task_id} 的 session_id 必须是字符串")

        return cls(
            id=task_id,
            category=category,
            session_id=session_id,
            messages=tuple(messages),
            expect=ExpectSpec.from_dict(raw.get("expect")),
            only_real_llm=only_real_llm,
            notes=str(raw.get("notes") or ""),
            profile_hint=raw.get("profile_hint"),
            inject=inject,
        )


def load_tasks(path: Path | str) -> list[TaskCase]:
    """读取 JSONL 任务集并校验 id 唯一。

    参数:
        path: tasks.jsonl 路径。

    返回:
        按文件顺序排列的 TaskCase 列表。

    异常:
        FileNotFoundError: 任务文件不存在。
        ValueError: 某行不是合法 JSON 对象、字段缺失，或 id 重复。
    """
    task_path = Path(path)
    text = task_path.read_text(encoding="utf-8")
    tasks: list[TaskCase] = []
    seen: set[str] = set()

    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"任务文件第 {line_no} 行不是合法 JSON: {exc}") from exc
        try:
            task = TaskCase.from_dict(raw)
        except ValueError as exc:
            raise ValueError(f"任务文件第 {line_no} 行非法: {exc}") from exc
        if task.id in seen:
            raise ValueError(f"任务 id 重复: {task.id}")
        seen.add(task.id)
        tasks.append(task)
    return tasks

"""任务集格式与数据类测试。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.task_schema import (  # noqa: E402
    VALID_CATEGORIES,
    ExpectSpec,
    TaskCase,
    load_tasks,
)


def _valid_raw(**overrides) -> dict:
    """构造一条合法任务字典，按需覆盖字段。"""
    raw = {
        "id": "sample-01",
        "category": "route_rule_hit",
        "session_id": "eval-sample-01",
        "messages": ["显示器多少钱"],
        "expect": {"route_agent": "pre_sales", "contains": ["1899"]},
        "notes": "示例任务",
    }
    raw.update(overrides)
    return raw


def test_task_case_parses_nested_expect():
    """合法任务应正确解析 ExpectSpec 与元组字段。"""
    task = TaskCase.from_dict(_valid_raw())

    assert task.id == "sample-01"
    assert task.messages == ("显示器多少钱",)
    assert isinstance(task.expect, ExpectSpec)
    assert task.expect.route_agent == "pre_sales"
    assert task.expect.contains == ("1899",)
    assert task.expect.no_write_tools is True
    assert task.expect.cache_hit is None
    assert task.expect.errors_allowed == ()


def test_missing_id_is_rejected():
    """缺少 id 必须拒绝，避免报告里出现不可定位的任务。"""
    raw = _valid_raw()
    raw.pop("id")
    with pytest.raises(ValueError, match="id"):
        TaskCase.from_dict(raw)


def test_empty_messages_are_rejected():
    """messages 为空数组必须拒绝（没有任何输入无法判定）。"""
    with pytest.raises(ValueError, match="messages"):
        TaskCase.from_dict(_valid_raw(messages=[]))


def test_unknown_category_is_rejected():
    """分类必须在约定集合内，否则分组统计会漂移。"""
    with pytest.raises(ValueError, match="category"):
        TaskCase.from_dict(_valid_raw(category="unknown"))


def test_unknown_injection_is_rejected():
    """未知注入类型必须尽早失败，避免静默不生效。"""
    with pytest.raises(ValueError, match="注入类型"):
        TaskCase.from_dict(_valid_raw(inject={"type": "magic"}))


def test_real_task_set_is_complete_and_unique():
    """校验仓库内任务集：数量、唯一性、分类覆盖与 only_real_llm 下限。"""
    tasks = load_tasks(ROOT / "evals" / "tasks.jsonl")

    assert len(tasks) >= 25
    ids = [task.id for task in tasks]
    assert len(ids) == len(set(ids))
    assert {task.category for task in tasks} == set(VALID_CATEGORIES)
    assert sum(1 for task in tasks if task.only_real_llm) >= 2
    assert all(task.messages for task in tasks)

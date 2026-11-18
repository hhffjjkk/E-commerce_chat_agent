"""Langfuse 可选上传：任务集 dataset 与评测分数。

设计原则:
    - 未配置 Langfuse（`is_enabled()` 为 False）时所有函数立即返回，不产生网络调用；
    - 任何异常都被吞掉并记录 `langfuse_eval_failed`，上传失败绝不影响评测本身；
    - dataset item 幂等：已存在则跳过。Langfuse 的 item 重复创建会覆盖 description，
      导致历史任务的 notes 被后来的重跑覆盖，破坏“任务集版本可追溯”。
"""

from __future__ import annotations

from agent_system.obs.events import log_event
from agent_system.obs.langfuse_client import get_langfuse, is_enabled

# dataset 名称固定：同名 dataset 会跨多次评测累积 item，便于按 task_id 比较趋势
DATASET_NAME = "ecommerce-agent-eval"


def _item_exists(client, item_id: str, cache: dict) -> bool:
    """判断 dataset item 是否已存在（best-effort，兼容不同 SDK 版本）。

    参数:
        client: Langfuse 客户端。
        item_id: 任务 id（即 item id）。
        cache: 已查到的 item id 集合，避免重复请求。

    返回:
        True 表示已存在。SDK 不支持查询时返回 False（退化为“尝试创建”）。
    """
    if item_id in cache:
        return True

    getter = getattr(client, "get_dataset_item", None)
    if callable(getter):
        try:
            if getter(item_id) is not None:
                cache.add(item_id)
                return True
        except Exception:  # noqa: BLE001 - 查询失败按“不存在”处理，交给创建兜底
            pass

    dataset_getter = getattr(client, "get_dataset", None)
    if callable(dataset_getter):
        try:
            dataset = dataset_getter(DATASET_NAME)
            items = getattr(dataset, "items", None) or []
            for item in items:
                existing_id = getattr(item, "id", None)
                if existing_id:
                    cache.add(str(existing_id))
            return item_id in cache
        except Exception:  # noqa: BLE001
            pass
    return False


def upload_dataset(tasks) -> int:
    """把任务集上传为 Langfuse dataset item（已存在则跳过）。

    参数:
        tasks: TaskCase 列表。

    返回:
        实际创建的 item 数量；未启用或失败时返回 0。
    """
    if not is_enabled():
        return 0

    client = get_langfuse()
    created = 0
    known_ids: set[str] = set()
    try:
        creator = getattr(client, "create_dataset", None)
        if callable(creator):
            try:
                creator(name=DATASET_NAME, description="固定离线评测任务集")
            except TypeError:
                # 兼容旧版位置参数签名
                creator(DATASET_NAME)

        item_creator = getattr(client, "create_dataset_item", None)
        if not callable(item_creator):
            return 0

        for task in tasks:
            if _item_exists(client, task.id, known_ids):
                continue
            item_creator(
                dataset_name=DATASET_NAME,
                id=task.id,
                input={
                    "messages": list(task.messages),
                    "session_id": task.session_id,
                    "category": task.category,
                },
                expected_output={
                    "route_agent": task.expect.route_agent,
                    "contains": list(task.expect.contains),
                    "no_write_tools": task.expect.no_write_tools,
                    "cache_hit": task.expect.cache_hit,
                    "errors_allowed": list(task.expect.errors_allowed),
                },
                description=task.notes,
            )
            known_ids.add(task.id)
            created += 1
    except Exception as exc:  # noqa: BLE001 - 上传是旁路能力
        log_event(
            "langfuse_eval_failed",
            action="upload_dataset",
            error_type=type(exc).__name__,
        )
    return created


def upload_scores(report, trace_ids: dict[str, str]) -> int:
    """把任务级评测分数上报到对应 Langfuse trace。

    参数:
        report: EvalReport。
        trace_ids: task_id → trace_id（由 collector 的 request_id 派生）。

    返回:
        实际上报的分数条数；未启用或失败时返回 0。

    说明:
        每个任务上报三个 score：pass_rate（0/1）、latency_ms、llm_calls。
        trace_id 缺失的任务直接跳过，不伪造 trace。
    """
    if not is_enabled():
        return 0

    client = get_langfuse()
    creator = getattr(client, "create_score", None)
    if not callable(creator):
        creator = getattr(client, "score", None)
    if not callable(creator):
        return 0

    count = 0
    try:
        for result in report.results:
            trace_id = trace_ids.get(result.task_id)
            if not trace_id:
                continue
            for name, value, data_type in (
                ("pass_rate", 1 if result.passed else 0, "BOOLEAN"),
                ("latency_ms", result.latency_ms, "NUMERIC"),
                ("llm_calls", result.llm_calls, "NUMERIC"),
            ):
                creator(
                    trace_id=trace_id,
                    name=name,
                    value=value,
                    data_type=data_type,
                )
                count += 1
    except Exception as exc:  # noqa: BLE001 - 上报失败不影响评测
        log_event(
            "langfuse_eval_failed",
            action="upload_scores",
            error_type=type(exc).__name__,
        )
    return count

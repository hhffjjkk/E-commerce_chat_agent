"""评测指标口径：任务结果、分组统计、P50/P95、成本与缓存命中率。

口径说明:
    - 通过率 = 通过任务数 / 实际执行任务数（被跳过的 only_real_llm 不计入分母）；
    - P50/P95 采用 nearest-rank（排序后取第 ceil(p*n) 个，不插值），
      按“任务级延迟”而非“LLM 调用级”计算：用户感知的是任务完成时间；
    - 成本单价来自配置（默认 0 表示不计算），不在代码里硬编码供应商价格。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from agent_system.eval.code_evaluators import (
    cache_hit_expected,
    contains_keywords,
    no_write_tools,
    route_match,
)

# evaluator 名 → 中文标签，报告与失败原因统一使用中文，避免英文 key 泄露到文档
EVALUATOR_LABELS: dict[str, str] = {
    "route_match": "路由匹配",
    "contains_keywords": "回复关键词",
    "no_write_tools": "未触发写工具",
    "cache_hit_expected": "缓存命中",
    "errors_allowed": "错误码白名单",
}


@dataclass
class TaskResult:
    """单条任务的评测结果。"""

    task_id: str
    category: str
    passed: bool
    route_agent: str | None
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    latency_ms: float
    tools_used: list[str]
    cache_hit: bool
    errors: list[str]
    evaluator_results: dict[str, bool]
    failure_reason: str | None
    notes: str = ""

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典。"""
        return asdict(self)


@dataclass
class LatencyStats:
    """任务级延迟统计（毫秒）。"""

    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass
class TokenStats:
    """Token 汇总。"""

    prompt: int
    completion: int


@dataclass
class CategoryStats:
    """按分类聚合的结果。"""

    total: int
    passed: int
    pass_rate: float
    avg_latency_ms: float
    p95_latency_ms: float
    avg_llm_calls: float


@dataclass
class ProfileStats:
    """按路由 Profile 聚合的结果。"""

    total: int
    passed: int
    pass_rate: float
    avg_latency_ms: float
    p95_latency_ms: float
    avg_llm_calls: float


@dataclass
class EvalReport:
    """一次完整评测的汇总。"""

    started_at: str
    finished_at: str
    mode: str
    seed: int
    total: int
    executed: int
    passed: int
    skipped: int
    pass_rate: float
    by_category: dict[str, CategoryStats]
    by_profile: dict[str, ProfileStats]
    latency: LatencyStats
    llm_calls_total: int
    tokens_total: TokenStats
    cost_usd_total: float | None
    cache_hit_count: int
    cache_miss_count: int
    cache_hit_rate: float
    results: list[TaskResult] = field(default_factory=list)
    skipped_tasks: list[dict] = field(default_factory=list)


def percentile(values, p: float) -> float:
    """nearest-rank 百分位：排序后取第 ceil(p/100*n) 个，不插值。

    参数:
        values: 数值序列（空序列返回 0.0，避免除零/索引错误）。
        p: 百分位，0~100。

    返回:
        对应百分位的数值；空输入返回 0.0。

    说明:
        不引入 numpy；nearest-rank 的语义是“有 p% 的样本不超过该值”，
        对小样本（评测常用 20~30 条任务）比线性插值更直观、更稳定。
    """
    data = sorted(float(value) for value in values)
    if not data:
        return 0.0
    rank = max(1, math.ceil(p / 100.0 * len(data)))
    rank = min(rank, len(data))
    return data[rank - 1]


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> float | None:
    """按千 token 单价计算成本。

    参数:
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        input_price_per_1k: 输入单价（每千 token）。
        output_price_per_1k: 输出单价（每千 token）。

    返回:
        美元成本；两个单价都为 0 时返回 None（表示未配置单价）。

    说明:
        单价由部署方按实际供应商填写：供应商定价会变，评测器不应耦合具体供应商。
    """
    if input_price_per_1k <= 0 and output_price_per_1k <= 0:
        return None
    cost = (
        prompt_tokens / 1000.0 * input_price_per_1k
        + completion_tokens / 1000.0 * output_price_per_1k
    )
    return round(cost, 6)


def _round(value: float) -> float:
    """统一保留两位小数，避免报告出现无意义的浮点尾数。"""
    return round(float(value), 2)


def _group_stats(results: list[TaskResult], key) -> dict[str, dict]:
    """按 key 分组并计算每组统计（总计/通过/通过率/延迟/P95/平均 LLM 调用）。"""
    groups: dict[str, list[TaskResult]] = {}
    for result in results:
        name = key(result)
        groups.setdefault(name, []).append(result)

    stats = {}
    for name, items in groups.items():
        latencies = [item.latency_ms for item in items]
        passed = sum(1 for item in items if item.passed)
        stats[name] = {
            "total": len(items),
            "passed": passed,
            "pass_rate": round(passed / len(items), 4) if items else 0.0,
            "avg_latency_ms": _round(sum(latencies) / len(latencies)) if latencies else 0.0,
            "p95_latency_ms": _round(percentile(latencies, 95)),
            "avg_llm_calls": (
                round(sum(item.llm_calls for item in items) / len(items), 2) if items else 0.0
            ),
        }
    return stats


def compute_task_result(
    *,
    task,
    snapshot: dict,
    reply: str,
    latency_ms: float,
    input_price_per_1k: float = 0.0,
    output_price_per_1k: float = 0.0,
) -> TaskResult:
    """把采集快照与回复折叠为一条 TaskResult。

    参数:
        task: TaskCase（提供 id/category/expect/notes）。
        snapshot: EvalCollector.stop_task() 返回的快照。
        reply: 最后一次 /chat 响应中的 reply 文本。
        latency_ms: 任务级实际耗时（毫秒）。
        input_price_per_1k / output_price_per_1k: 成本单价；0 表示未配置。

    返回:
        TaskResult；`passed` 为所有适用 evaluator 均通过。

    说明:
        evaluator 的“适用”条件由各自函数决定（期望字段为 None/空即不适用）；
        错误码白名单单独作为一条 evaluator，便于失败原因直接展示缺失的错误。
    """
    expect = task.expect
    route = snapshot.get("route") or {}
    tools_used = list(snapshot.get("tools_used") or [])
    cache_hit = int(snapshot.get("cache_hit") or 0) > 0
    errors = list(snapshot.get("errors") or [])

    evaluator_results: dict[str, bool] = {}
    for score in (
        route_match(route.get("agent"), expect.route_agent),
        contains_keywords(reply, expect.contains),
        no_write_tools(tools_used, expected=expect.no_write_tools),
        cache_hit_expected(cache_hit, expect.cache_hit),
    ):
        if score is not None:
            evaluator_results[score["name"]] = bool(score["value"])

    # 错误码白名单：默认空列表表示不允许任何错误；
    # 任务显式列出后，出现白名单外的错误仍然失败。
    allowed = set(expect.errors_allowed)
    evaluator_results["errors_allowed"] = set(errors) <= allowed

    failed = [name for name, ok in evaluator_results.items() if not ok]
    failure_reason = None
    if failed:
        failure_reason = "；".join(EVALUATOR_LABELS.get(name, name) + "未通过" for name in failed)
        if "errors_allowed" in failed and errors:
            failure_reason += f"（实际错误码: {', '.join(errors)}）"

    return TaskResult(
        task_id=task.id,
        category=task.category,
        passed=not failed,
        route_agent=route.get("agent"),
        llm_calls=int(snapshot.get("llm_calls") or 0),
        prompt_tokens=int(snapshot.get("prompt_tokens") or 0),
        completion_tokens=int(snapshot.get("completion_tokens") or 0),
        cost_usd=compute_cost(
            int(snapshot.get("prompt_tokens") or 0),
            int(snapshot.get("completion_tokens") or 0),
            input_price_per_1k,
            output_price_per_1k,
        ),
        latency_ms=_round(latency_ms),
        tools_used=tools_used,
        cache_hit=cache_hit,
        errors=errors,
        evaluator_results=evaluator_results,
        failure_reason=failure_reason,
        notes=task.notes,
    )


def compute_report(
    *,
    mode: str,
    seed: int,
    started_at: str,
    finished_at: str,
    results: list[TaskResult],
    skipped_tasks: list[dict],
    cache_hit_count: int,
    cache_miss_count: int,
) -> EvalReport:
    """汇总所有任务结果，生成 EvalReport。

    参数:
        mode: "mock" 或 "real"。
        seed: 固定随机种子（记录以便复现）。
        started_at / finished_at: ISO 时间字符串。
        results: 已执行任务的结果列表。
        skipped_tasks: 被跳过的任务（only_real_llm）。
        cache_hit_count / cache_miss_count: 全部请求的缓存命中/未命中次数。

    返回:
        EvalReport；空 results 时通过率与延迟均为 0，不抛异常。
    """
    executed = len(results)
    passed = sum(1 for item in results if item.passed)
    latencies = [item.latency_ms for item in results]

    by_category = {
        name: CategoryStats(**stats)
        for name, stats in _group_stats(results, lambda item: item.category).items()
    }
    by_profile = {
        name: ProfileStats(**stats)
        for name, stats in _group_stats(
            results, lambda item: item.route_agent or "未知"
        ).items()
    }

    costs = [item.cost_usd for item in results if item.cost_usd is not None]
    total_cache = cache_hit_count + cache_miss_count
    return EvalReport(
        started_at=started_at,
        finished_at=finished_at,
        mode=mode,
        seed=seed,
        total=executed + len(skipped_tasks),
        executed=executed,
        passed=passed,
        skipped=len(skipped_tasks),
        pass_rate=round(passed / executed, 4) if executed else 0.0,
        by_category=by_category,
        by_profile=by_profile,
        latency=LatencyStats(
            p50_ms=_round(percentile(latencies, 50)),
            p95_ms=_round(percentile(latencies, 95)),
            max_ms=_round(max(latencies)) if latencies else 0.0,
        ),
        llm_calls_total=sum(item.llm_calls for item in results),
        tokens_total=TokenStats(
            prompt=sum(item.prompt_tokens for item in results),
            completion=sum(item.completion_tokens for item in results),
        ),
        cost_usd_total=round(sum(costs), 6) if costs else None,
        cache_hit_count=cache_hit_count,
        cache_miss_count=cache_miss_count,
        cache_hit_rate=round(cache_hit_count / total_cache, 4) if total_cache else 0.0,
        results=results,
        skipped_tasks=list(skipped_tasks),
    )

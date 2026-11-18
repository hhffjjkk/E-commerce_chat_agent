"""并发压测的指标聚合：请求记录、并发度统计与场景结果。

口径说明:
    - QPS = 成功请求数 / 实际耗时（最后一个请求结束 - 第一个请求开始），
      不是“成功数 / 发出数”：错误请求不能算进吞吐；
    - P50/P95/P99 采用 nearest-rank（与步骤 1 的评测口径一致），不插值；
    - 延迟与 TTFB 只统计成功请求：失败请求的耗时不能代表正常链路；
    - 缓存命中率 = 命中数 / 总请求数，分母包含失败请求；
    - Mock 模式下 LLM 调用恒为 0，跨请求的 LLM 节省量需真实模式或外部估算。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from agent_system.eval.load_scenarios import LoadScenario


@dataclass
class RequestRecord:
    """单个压测请求的记录。"""

    started_at: float
    finished_at: float
    status_code: int
    latency_ms: float
    ttfb_ms: float | None = None
    cache_hit: bool = False
    llm_calls: int = 0
    route_agent: str | None = None
    error: str | None = None


@dataclass
class LevelStats:
    """某个并发度下的聚合指标。"""

    concurrency: int
    total_requests: int
    success: int
    errors: int
    error_rate: float
    qps: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    ttfb_p50_ms: float | None
    ttfb_p95_ms: float | None
    cache_hit_rate: float
    avg_llm_calls_per_request: float
    total_llm_calls: int


@dataclass
class LoadScenarioResult:
    """一个场景在全部并发度下的结果。

    额外字段 mode / seed / environment 用于报告与复现：
    JSON 根节点是场景结果列表，这些信息随每条结果一起保存，避免再包一层。
    """

    scenario: LoadScenario
    levels: list[LevelStats]
    wall_clock_ms: float
    aborted: str | None
    mode: str = "mock"
    seed: int = 42
    environment: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转为可 JSON 序列化的字典。"""
        payload = asdict(self)
        payload["scenario"]["concurrency_levels"] = list(
            self.scenario.concurrency_levels
        )
        return payload

    def highest_level(self) -> LevelStats | None:
        """返回最高并发度的统计；没有数据时返回 None。"""
        return max(self.levels, key=lambda level: level.concurrency) if self.levels else None


def nearest_rank_percentile(values, p: float) -> float:
    """nearest-rank 百分位：排序后取第 ceil(p/100*n) 个，不插值。

    参数:
        values: 数值序列；空序列返回 0.0。
        p: 百分位，0~100。

    返回:
        对应百分位的数值。
    """
    data = sorted(float(value) for value in values)
    if not data:
        return 0.0
    rank = max(1, math.ceil(p / 100.0 * len(data)))
    rank = min(rank, len(data))
    return data[rank - 1]


def compute_wall_clock_ms(records: list[RequestRecord]) -> float:
    """计算实际耗时：最后一个请求结束 - 第一个请求开始。

    参数:
        records: 请求记录列表。

    返回:
        毫秒；空列表返回 0.0。
    """
    if not records:
        return 0.0
    # time.monotonic() 的单位是秒，这里统一换算成毫秒
    return (
        max(record.finished_at for record in records)
        - min(record.started_at for record in records)
    ) * 1000.0


def compute_qps(success: int, wall_clock_ms: float) -> float:
    """按成功请求数与实际耗时计算 QPS。

    参数:
        success: 成功请求数。
        wall_clock_ms: 实际耗时（毫秒）。

    返回:
        QPS，保留两位小数；耗时 <= 0 时返回 0.0。

    说明:
        用成功数而不是发出数：把 500/超时算进吞吐会虚高 QPS。
    """
    if success <= 0 or wall_clock_ms <= 0:
        return 0.0
    return round(success / (wall_clock_ms / 1000.0), 2)


def _round(value: float) -> float:
    """统一保留两位小数。"""
    return round(float(value), 2)


def aggregate_level(records: list[RequestRecord], concurrency: int) -> LevelStats:
    """把一个并发度下的请求记录聚合为 LevelStats。

    参数:
        records: 请求记录。
        concurrency: 并发度。

    返回:
        LevelStats；空请求时所有比率与百分位均为 0，不抛异常。
    """
    total = len(records)
    success_records = [
        record
        for record in records
        if record.error is None and 200 <= record.status_code < 300
    ]
    success = len(success_records)
    errors = total - success
    latencies = [record.latency_ms for record in success_records]
    ttfb_values = [
        record.ttfb_ms for record in success_records if record.ttfb_ms is not None
    ]

    return LevelStats(
        concurrency=concurrency,
        total_requests=total,
        success=success,
        errors=errors,
        error_rate=round(errors / total, 4) if total else 0.0,
        qps=compute_qps(success, compute_wall_clock_ms(records)),
        latency_p50_ms=_round(nearest_rank_percentile(latencies, 50)),
        latency_p95_ms=_round(nearest_rank_percentile(latencies, 95)),
        latency_p99_ms=_round(nearest_rank_percentile(latencies, 99)),
        latency_max_ms=_round(max(latencies)) if latencies else 0.0,
        # TTFB 只对存在该字段的请求聚合；全无 TTFB 时保持 None 而不是伪造 0
        ttfb_p50_ms=(
            _round(nearest_rank_percentile(ttfb_values, 50)) if ttfb_values else None
        ),
        ttfb_p95_ms=(
            _round(nearest_rank_percentile(ttfb_values, 95)) if ttfb_values else None
        ),
        cache_hit_rate=(
            round(sum(1 for record in records if record.cache_hit) / total, 4)
            if total
            else 0.0
        ),
        avg_llm_calls_per_request=(
            round(sum(record.llm_calls for record in records) / total, 2)
            if total
            else 0.0
        ),
        total_llm_calls=sum(record.llm_calls for record in records),
    )


def estimate_saved_llm_calls(
    cache_hit_count: int, baseline_avg_llm_calls: float | None
) -> float | None:
    """估算缓存命中节省的 LLM 调用量。

    参数:
        cache_hit_count: 缓存命中次数。
        baseline_avg_llm_calls: 无缓存基线下“平均每请求 LLM 调用数”。

    返回:
        估算值；基线缺失时返回 None（无法估算）。

    说明:
        该估算依赖 cache_off 基线；没有基线就无法知道一次命中原本要花多少次调用。
        Mock 模式下基线为 0，因此结果恒为 0——不能当成真实收益。
    """
    if baseline_avg_llm_calls is None:
        return None
    return round(cache_hit_count * baseline_avg_llm_calls, 2)

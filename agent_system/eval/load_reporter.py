"""并发压测报告：JSON（程序化对比）与 Markdown（人读 / 面试材料）。

与步骤 1 的评测报告并列存放，前缀不同：
    `eval-*` 是正确性评测，`load-*` 是并发压测，二者不互相覆盖。

摘要规则:
    只有同时存在 cache_off 基线且基线 QPS > 0 时才生成收益对比；
    否则明确写“未运行基线场景，无法生成对比摘要”。
    失败/中止场景必须列出，百分比保留一位小数。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from agent_system.config import PROJECT_ROOT
from agent_system.eval.load_metrics import LoadScenarioResult, LevelStats
from agent_system.eval.load_scenarios import MOCK_LLM_ORCHESTRATION_MS

_DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


def _file_stem(started_at: str, mode: str) -> str:
    """由运行开始时间生成报告文件名前缀。

    参数:
        started_at: ISO 时间字符串。
        mode: mock / real。

    返回:
        `load-<yyyyMMdd-HHmmss>-<mode>`。
    """
    try:
        stamp = datetime.fromisoformat(started_at).strftime("%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"load-{stamp}-{mode}"


def _ensure_dir(out_dir: Path | str | None) -> Path:
    """创建并返回报告目录。"""
    path = Path(out_dir) if out_dir is not None else _DEFAULT_REPORTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cell(text) -> str:
    """转义 Markdown 表格单元格。"""
    return str(text).replace("|", "｜").replace("\n", " ")


def _ms(value: float | None) -> str:
    """延迟格式化；None 显示为“—”。"""
    return "—" if value is None else f"{value:.1f}"


def _pct(value: float) -> str:
    """比例格式化为一位小数百分比。"""
    return f"{value * 100:.1f}%"


def _change(old: float, new: float) -> float:
    """变化百分比：正数表示提升/降低，负数表示反向变化。"""
    if old <= 0:
        return 0.0
    return (new - old) / old * 100.0


def _highest(result: LoadScenarioResult) -> LevelStats | None:
    """取场景最高并发度的统计。"""
    return result.highest_level()


def _total_requests(results: list[LoadScenarioResult]) -> int:
    """所有场景、所有并发度的请求总数。"""
    return sum(level.total_requests for result in results for level in result.levels)


def write_json_report(
    results: list[LoadScenarioResult],
    *,
    mode: str,
    seed: int,
    started_at: str,
    finished_at: str,
    out_dir: Path | str | None = None,
) -> Path:
    """写出 JSON 压测报告（根节点为场景结果列表）。

    参数:
        results: 场景结果。
        mode: mock / real。
        seed: 随机种子。
        started_at / finished_at: ISO 时间。
        out_dir: 输出目录。

    返回:
        报告路径。

    说明:
        根节点保持列表，便于直接用 `list[LoadScenarioResult]` 反序列化；
        mode/seed 已写入每个场景结果，不会再丢失运行上下文。
    """
    path = _ensure_dir(out_dir) / f"{_file_stem(started_at, mode)}.json"
    payload = [result.to_dict() for result in results]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def build_summary_paragraph(
    results: list[LoadScenarioResult], *, mode: str
) -> str:
    """生成缓存收益摘要段落。

    参数:
        results: 场景结果列表。
        mode: mock / real。

    返回:
        3–5 行中文摘要；缺少基线时返回明确说明。

    说明:
        只做有数据支撑的结论：QPS/P95/命中率均来自最高并发度；
        若启用缓存的 QPS 反而更低，如实显示负增长，不粉饰。
    """
    baseline = next((item for item in results if item.scenario.name == "cache_off"), None)
    candidates = [
        item
        for item in results
        if item.scenario.cache_enabled and item.scenario.name != "cache_off"
    ]
    if baseline is None or not candidates:
        return "未运行基线场景，无法生成对比摘要。"

    base_level = _highest(baseline)
    # 优先使用 50% 重复场景；没有时取第一个启用缓存的场景
    enabled = next(
        (
            item
            for item in candidates
            if item.scenario.name == "cache_on_repeat_0.5"
        ),
        candidates[0],
    )
    on_level = _highest(enabled)
    if base_level is None or on_level is None or base_level.qps <= 0:
        return "未运行基线场景，无法生成对比摘要。"

    qps_change = _change(base_level.qps, on_level.qps)
    p95_change = _change(base_level.latency_p95_ms, on_level.latency_p95_ms)
    # P95 “降低”用 (旧-新)/旧；数值为负表示延迟反而升高
    p95_reduction = -p95_change if base_level.latency_p95_ms > 0 else 0.0
    qps_text = (
        f"提升 {qps_change:.1f}%" if qps_change >= 0 else f"下降 {-qps_change:.1f}%"
    )
    p95_text = (
        f"降低 {p95_reduction:.1f}%"
        if p95_reduction >= 0
        else f"上升 {-p95_reduction:.1f}%"
    )

    lines = [
        (
            f"在 {base_level.concurrency} 并发下，同一消息池启用归一化消息缓存"
            f"（重复比例 {enabled.scenario.repeat_ratio:.2f}）使 "
            f"{enabled.scenario.target_path} 的 QPS 从 {base_level.qps:.1f} "
            f"提升至 {on_level.qps:.1f}（{qps_text}），"
        ),
        (
            f"P95 延迟从 {base_level.latency_p95_ms:.1f} ms 变为 "
            f"{on_level.latency_p95_ms:.1f} ms（{p95_text}），"
            f"缓存命中率 {on_level.cache_hit_rate * 100:.1f}%。"
        ),
    ]
    if mode == "mock":
        lines.append(
            "注：本次压测在 mock 模式下运行，LLM 调用量恒为 0；"
            f"cache-miss 请求额外注入 {MOCK_LLM_ORCHESTRATION_MS:.1f} ms "
            "模拟编排耗时，绝对 QPS 仅用于相对比较。"
        )
    else:
        lines.append(
            f"注：本次压测在 real 模式下运行，"
            f"LLM 调用量从 {base_level.total_llm_calls} 降至 {on_level.total_llm_calls}。"
        )

    failures = [
        item
        for item in results
        if item.aborted
        or any(level.errors > 0 for level in item.levels)
    ]
    if failures:
        parts = []
        for item in failures:
            if item.aborted:
                parts.append(f"{item.scenario.name} 中止（{item.aborted}）")
            else:
                errors = sum(level.errors for level in item.levels)
                parts.append(f"{item.scenario.name} 有 {errors} 个错误请求")
        lines.append("失败/中止：" + "；".join(parts) + "。")
    return "\n".join(lines)


def write_markdown_report(
    results: list[LoadScenarioResult],
    *,
    mode: str,
    seed: int,
    started_at: str,
    finished_at: str,
    out_dir: Path | str | None = None,
) -> Path:
    """写出 Markdown 压测报告。

    参数:
        results: 场景结果。
        mode: mock / real。
        seed: 随机种子。
        started_at / finished_at: ISO 时间。
        out_dir: 输出目录。

    返回:
        报告路径。
    """
    path = _ensure_dir(out_dir) / f"{_file_stem(started_at, mode)}.md"
    lines: list[str] = [
        f"# 并发压测报告（{mode}）",
        "",
        "## 1. 运行信息",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| 开始时间 | {started_at} |",
        f"| 结束时间 | {finished_at} |",
        f"| 运行模式 | {mode} |",
        f"| 随机种子 | {seed} |",
        (
            f"| Mock 模拟编排耗时 | {MOCK_LLM_ORCHESTRATION_MS:.1f} ms（仅 mock） |"
            if mode == "mock"
            else "| Mock 模拟编排耗时 | —（real 模式为真实 LLM 延迟） |"
        ),
        f"| 场景数 | {len(results)} |",
        f"| 总请求数 | {_total_requests(results)} |",
        "",
        "## 2. 场景对比（最高并发度）",
        "",
        "| 场景 | 路径 | 缓存 | 重复比例 | 最高并发 | QPS | P95(ms) | 缓存命中率 | 错误率 | 中止 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        level = _highest(result)
        if level is None:
            continue
        hit_text = _pct(level.cache_hit_rate) if result.scenario.cache_enabled else "N/A"
        lines.append(
            f"| {_cell(result.scenario.name)} | {result.scenario.target_path} | "
            f"{'开启' if result.scenario.cache_enabled else '关闭'} | "
            f"{result.scenario.repeat_ratio:.2f} | {level.concurrency} | "
            f"{level.qps:.2f} | {_ms(level.latency_p95_ms)} | {hit_text} | "
            f"{_pct(level.error_rate)} | {_cell(result.aborted or '无')} |"
        )

    lines += ["", "## 3. 并发度明细", ""]
    for result in results:
        lines += [f"### {result.scenario.name}", ""]
        lines += [
            "| 并发度 | 请求数 | 成功 | 错误率 | QPS | P50(ms) | P95(ms) | P99(ms) | 最大(ms) | TTFB P50(ms) | TTFB P95(ms) | 缓存命中率 | 平均LLM调用 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for level in result.levels:
            hit_text = _pct(level.cache_hit_rate) if result.scenario.cache_enabled else "N/A"
            lines.append(
                f"| {level.concurrency} | {level.total_requests} | {level.success} | "
                f"{_pct(level.error_rate)} | {level.qps:.2f} | "
                f"{_ms(level.latency_p50_ms)} | {_ms(level.latency_p95_ms)} | "
                f"{_ms(level.latency_p99_ms)} | {_ms(level.latency_max_ms)} | "
                f"{_ms(level.ttfb_p50_ms)} | {_ms(level.ttfb_p95_ms)} | "
                f"{hit_text} | {level.avg_llm_calls_per_request:.2f} |"
            )
        lines.append("")

    lines += ["## 4. 缓存收益对比", ""]
    baseline = next((item for item in results if item.scenario.name == "cache_off"), None)
    enabled_results = [
        item
        for item in results
        if item.scenario.cache_enabled and item.scenario.name != "cache_off"
    ]
    if baseline is None or not enabled_results:
        lines.append("未运行基线场景，无法生成对比摘要。")
    else:
        base_level = _highest(baseline)
        lines += [
            "| 场景 | QPS 提升 | P95 降低 | LLM 调用削减 |",
            "|---|---|---|---|",
        ]
        for item in enabled_results:
            level = _highest(item)
            if base_level is None or level is None or base_level.qps <= 0:
                continue
            qps_change = _change(base_level.qps, level.qps)
            p95_reduction = (
                -_change(base_level.latency_p95_ms, level.latency_p95_ms)
                if base_level.latency_p95_ms > 0
                else 0.0
            )
            if base_level.total_llm_calls <= 0:
                llm_text = "需真实 LLM 模式"
            else:
                llm_change = -_change(
                    base_level.total_llm_calls, level.total_llm_calls
                )
                llm_text = f"{llm_change:.1f}%"
            lines.append(
                f"| {_cell(item.scenario.name)} | {qps_change:.1f}% | "
                f"{p95_reduction:.1f}% | {_cell(llm_text)} |"
            )

    lines += ["", "## 5. 摘要", "", build_summary_paragraph(results, mode=mode), ""]

    lines += [
        "## 6. 失败/中止项",
        "",
        "| 场景 | 并发度 | 错误数 | 中止原因 | 说明 |",
        "|---|---|---|---|---|",
    ]
    failure_rows = 0
    for item in results:
        for level in item.levels:
            if level.errors > 0:
                failure_rows += 1
                lines.append(
                    f"| {_cell(item.scenario.name)} | {level.concurrency} | "
                    f"{level.errors} | {_cell(item.aborted or '无')} | "
                    f"错误率 {_pct(level.error_rate)} |"
                )
        if item.aborted and not item.levels:
            failure_rows += 1
            lines.append(
                f"| {_cell(item.scenario.name)} | - | 0 | {item.aborted} | 场景提前中止 |"
            )
    if failure_rows == 0:
        lines.append("| - | - | 0 | 无 | 未出现失败或中止 |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

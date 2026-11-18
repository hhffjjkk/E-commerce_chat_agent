"""评测报告生成：JSON（供程序化对比）与 Markdown（供人读 / 面试文档）。

为什么同时输出两种:
    JSON 结构稳定，适合 CI 对比与后续版本 diff；Markdown 可读性高，
    能直接贴进简历或面试文档。两者来自同一个 EvalReport，避免口径漂移。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from agent_system.config import PROJECT_ROOT
from agent_system.eval.metrics import EVALUATOR_LABELS, EvalReport

_DEFAULT_REPORTS_DIR = PROJECT_ROOT / "reports"


def _file_stem(report: EvalReport) -> str:
    """由评测开始时间生成稳定文件名，保证 JSON 与 Markdown 成对出现。

    参数:
        report: 评测报告。

    返回:
        `eval-<yyyyMMdd-HHmmss>-<mode>`。

    说明:
        解析失败时退化为当前时间：报告文件名不应成为评测失败的原因。
    """
    try:
        stamp = datetime.fromisoformat(report.started_at).strftime("%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"eval-{stamp}-{report.mode}"


def _ensure_dir(out_dir: Path | str | None) -> Path:
    """创建并返回报告目录。"""
    path = Path(out_dir) if out_dir is not None else _DEFAULT_REPORTS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(report: EvalReport, out_dir: Path | str | None = None) -> Path:
    """写出 JSON 报告并返回路径。

    参数:
        report: 评测报告。
        out_dir: 输出目录；None 时使用项目根目录下的 reports/。
    """
    path = _ensure_dir(out_dir) / f"{_file_stem(report)}.json"
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _pct(value: float) -> str:
    """把 0~1 的比例格式化为一位小数的百分比。"""
    return f"{value * 100:.1f}%"


def _ms(value: float) -> str:
    """延迟格式化：保留一位小数。"""
    return f"{value:.1f}"


def _cost(report: EvalReport) -> str:
    """成本展示：未配置单价时明确写出，而不是显示 0 造成误解。"""
    if report.cost_usd_total is None:
        return "未配置单价"
    return f"${report.cost_usd_total:.4f}"


def _evaluator_text(result) -> str:
    """把 evaluator 结果渲染成中文“名称=通过/失败”串。"""
    parts = []
    for name, ok in result.evaluator_results.items():
        label = EVALUATOR_LABELS.get(name, name)
        parts.append(f"{label}={'通过' if ok else '失败'}")
    return "，".join(parts) if parts else "-"


def _cell(text) -> str:
    """转义表格单元格，避免备注里的竖线破坏 Markdown 表格。"""
    return str(text).replace("|", "｜").replace("\n", " ")


def _summary(report: EvalReport) -> list[str]:
    """生成 3–5 行中文摘要。

    规则:
        不夸大、不隐藏失败任务：失败数量与失败分类必须写出；
        未配置单价时主动说明，避免读者把“无成本”误读为“零成本”。
    """
    failed = [item for item in report.results if not item.passed]
    failed_categories = sorted({item.category for item in failed})
    avg_llm = (
        report.llm_calls_total / report.executed if report.executed else 0.0
    )

    lines = [
        (
            f"本次评测共 {report.total} 个任务（实际执行 {report.executed} 个，"
            f"跳过 {report.skipped} 个），通过率 {_pct(report.pass_rate)}"
            f"（{report.passed}/{report.executed}）。"
        ),
        (
            f"平均每任务 {avg_llm:.1f} 次 LLM 调用，P50 延迟 {_ms(report.latency.p50_ms)} ms，"
            f"P95 延迟 {_ms(report.latency.p95_ms)} ms。"
        ),
        (
            f"缓存命中 {report.cache_hit_count} 次、未命中 {report.cache_miss_count} 次，"
            f"命中率 {_pct(report.cache_hit_rate)}；"
            f"成本：{_cost(report)}。"
        ),
    ]
    if failed:
        lines.append(
            f"失败任务 {len(failed)} 个，分类：{', '.join(failed_categories)}。"
        )
    else:
        lines.append("失败任务 0 个。")
    return lines


def write_markdown(report: EvalReport, out_dir: Path | str | None = None) -> Path:
    """写出 Markdown 报告并返回路径。

    参数:
        report: 评测报告。
        out_dir: 输出目录；None 时使用项目根目录下的 reports/。

    说明:
        结构固定为 7 个区块 + 末尾摘要；新增指标时优先扩展表格列，
        不要打乱区块顺序，便于跨版本肉眼对比。
    """
    path = _ensure_dir(out_dir) / f"{_file_stem(report)}.md"
    avg_llm = report.llm_calls_total / report.executed if report.executed else 0.0
    saved_calls = report.cache_hit_count * avg_llm

    lines: list[str] = [
        f"# 评测报告（{report.mode}）",
        "",
        "## 1. 运行信息",
        "",
        "| 项目 | 值 |",
        "|---|---|",
        f"| 开始时间 | {report.started_at} |",
        f"| 结束时间 | {report.finished_at} |",
        f"| 运行模式 | {report.mode} |",
        f"| 随机种子 | {report.seed} |",
        f"| 任务总数 | {report.total} |",
        f"| 实际执行 | {report.executed} |",
        f"| 跳过（仅真实 LLM） | {report.skipped} |",
        f"| 通过率 | {_pct(report.pass_rate)}（{report.passed}/{report.executed}） |",
        "",
        "## 2. 核心指标",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 通过率 | {_pct(report.pass_rate)} |",
        f"| 平均每任务 LLM 调用 | {avg_llm:.2f} |",
        f"| P50 延迟（任务级） | {_ms(report.latency.p50_ms)} ms |",
        f"| P95 延迟（任务级） | {_ms(report.latency.p95_ms)} ms |",
        f"| 最大延迟 | {_ms(report.latency.max_ms)} ms |",
        f"| Token 用量（输入/输出） | {report.tokens_total.prompt} / {report.tokens_total.completion} |",
        f"| 缓存命中率 | {_pct(report.cache_hit_rate)} |",
        f"| 总成本 | {_cost(report)} |",
        "",
        "## 3. 按分类统计",
        "",
        "| 分类 | 任务数 | 通过 | 通过率 | 平均延迟(ms) | P95(ms) | 平均LLM调用 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, stat in sorted(report.by_category.items()):
        lines.append(
            f"| {name} | {stat.total} | {stat.passed} | {_pct(stat.pass_rate)} | "
            f"{_ms(stat.avg_latency_ms)} | {_ms(stat.p95_latency_ms)} | {stat.avg_llm_calls:.2f} |"
        )

    lines += [
        "",
        "## 4. 按 Profile 统计",
        "",
        "| Profile | 任务数 | 通过 | 通过率 | 平均延迟(ms) | P95(ms) | 平均LLM调用 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, stat in sorted(report.by_profile.items()):
        lines.append(
            f"| {name} | {stat.total} | {stat.passed} | {_pct(stat.pass_rate)} | "
            f"{_ms(stat.avg_latency_ms)} | {_ms(stat.p95_latency_ms)} | {stat.avg_llm_calls:.2f} |"
        )

    failed = [item for item in report.results if not item.passed]
    lines += ["", "## 5. 失败任务清单", ""]
    if failed:
        lines += [
            "| task_id | 分类 | 失败原因 | evaluator 结果 | 说明 |",
            "|---|---|---|---|---|",
        ]
        for item in failed:
            lines.append(
                f"| {_cell(item.task_id)} | {_cell(item.category)} | "
                f"{_cell(item.failure_reason or '-')} | {_cell(_evaluator_text(item))} | "
                f"{_cell(item.notes or '-')} |"
            )
    else:
        lines.append("无失败任务。")

    lines += [
        "",
        "## 6. 缓存命中节省的 LLM 调用（估算）",
        "",
        (
            f"估算值 = 缓存命中次数 × 平均每任务 LLM 调用数 = "
            f"{report.cache_hit_count} × {avg_llm:.2f} ≈ {saved_calls:.1f} 次。"
        ),
        "",
        "局限：该口径假设每次命中都等价于一次完整任务的全部 LLM 调用；"
        "如果同一任务被重复命中，会高估节省量。它只用于横向趋势对比，不作为账单依据。",
        "",
        "## 7. 任务清单（含测试意图）",
        "",
        "| task_id | 分类 | 结果 | 说明 |",
        "|---|---|---|---|",
    ]
    for item in report.results:
        lines.append(
            f"| {_cell(item.task_id)} | {_cell(item.category)} | "
            f"{'通过' if item.passed else '失败'} | {_cell(item.notes or '-')} |"
        )
    for item in report.skipped_tasks:
        lines.append(
            f"| {_cell(item.get('task_id'))} | {_cell(item.get('category'))} | "
            f"跳过（{_cell(item.get('reason') or '仅真实 LLM')}） | "
            f"{_cell(item.get('notes') or '-')} |"
        )

    lines += ["", "## 摘要", ""]
    lines += _summary(report)
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path

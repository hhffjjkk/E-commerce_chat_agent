#!/usr/bin/env python
"""从 reports/ 汇总关键指标，生成可追溯的 metrics-index.json 与 .md。

为什么解析 JSON 而不是 Markdown:
    Markdown 是给人读的，列宽、措辞、表格格式随时可能调整；
    JSON 字段稳定，适合程序化提取与 CI 校验。

为什么 metrics-index.json 纳入版本控制:
    它是简历与对比报告中每个数字的“来源索引”；报告可以重跑，
    但“某个数字当时来自哪份报告、哪条记录”必须可追溯。

缺失数据的原则:
    任何指标在报告缺失时标记为 null，并在 caveat 写明“数据缺失”，
    绝不静默跳过或用估算值补位。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REPORTS = ROOT / "reports"
DEFAULT_OUT = ROOT / "docs" / "resume"


def code_version() -> str:
    """返回当前代码的 git 短哈希；不可用时返回 "unknown"。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - 版本锚点不可用不应阻塞汇总
        return "unknown"


def _read_json(path: Path | None):
    """读取 JSON；路径为空或解析失败时返回 None。"""
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _latest(reports_dir: Path, pattern: str) -> Path | None:
    """按修改时间取最新匹配文件。"""
    files = sorted(reports_dir.glob(pattern), key=lambda item: item.stat().st_mtime)
    return files[-1] if files else None


def _scenario(load_data, name: str):
    """在 load 报告列表中查找指定场景。"""
    if not isinstance(load_data, list):
        return None
    for item in load_data:
        if isinstance(item, dict) and (item.get("scenario") or {}).get("name") == name:
            return item
    return None


def _highest(scenario: dict | None):
    """取场景最高并发度的统计。"""
    if not scenario:
        return None
    levels = scenario.get("levels") or []
    if not levels:
        return None
    return max(levels, key=lambda level: level.get("concurrency", 0))


def _category_rate(eval_data, category: str):
    """取 EvalReport.by_category 中某个分类的通过率（0~1）。"""
    if not isinstance(eval_data, dict):
        return None
    stats = (eval_data.get("by_category") or {}).get(category)
    return None if not stats else stats.get("pass_rate")


def _avg_llm_calls(eval_data):
    """取单任务平均 LLM 调用次数。"""
    if not isinstance(eval_data, dict):
        return None
    executed = eval_data.get("executed") or 0
    if executed <= 0:
        return None
    return (eval_data.get("llm_calls_total") or 0) / executed


def _pct(value: float | None) -> float | None:
    """把 0~1 的比例转成百分比并保留一位小数。"""
    return None if value is None else round(value * 100.0, 1)


def _rel(path: Path | None) -> str:
    """把报告路径转成相对仓库根的 POSIX 形式，跨平台可读。"""
    if path is None:
        return ""
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _delta(current: float | None, baseline: float | None) -> float | None:
    """两个百分比之间的差值（百分点）。"""
    if current is None or baseline is None:
        return None
    return round(current - baseline, 1)


def _entry(
    entry_id: str,
    metric: str,
    value,
    unit: str,
    display: str,
    source_type: str,
    source_file: str,
    source_field: str,
    condition: str,
    caveat: str,
) -> dict:
    """构造一条指标索引记录。"""
    return {
        "id": entry_id,
        "metric": metric,
        "value": value,
        "unit": unit,
        "display": display,
        "source_type": source_type,
        "source_file": source_file,
        "source_field": source_field,
        "condition": condition,
        "caveat": caveat,
    }


def _pct_display(value: float | None, suffix: str = "") -> str:
    """百分比展示：一位小数；缺失显示“—”。"""
    return "—" if value is None else f"{value:.1f}%{suffix}"


def _qps_display(value: float | None) -> str:
    """QPS 展示：保留两位小数；缺失显示“—”。"""
    return "—" if value is None else f"{value:.2f} QPS"


def _count_display(value: float | None) -> str:
    """次数展示：整数不带小数；缺失显示“—”。"""
    return "—" if value is None else f"{int(round(value))} 次/任务"


def build_entries(reports_dir: Path, version: str) -> list[dict]:
    """从报告目录构建全部预定义指标条目。

    参数:
        reports_dir: reports/ 目录。
        version: 代码版本（写入 JSON 顶层，不进入条目）。

    返回:
        指标条目列表；缺失数据以 value=None 与 caveat 说明呈现。
    """
    eval_path = _latest(reports_dir, "eval-*.json")
    load_path = _latest(reports_dir, "load-*.json")
    no_multi_path = _latest(reports_dir, "baseline-no_multi_agent-*.json")
    no_retry_path = _latest(reports_dir, "baseline-no_retry-*.json")
    no_guard_path = _latest(reports_dir, "baseline-no_guardrails-*.json")
    no_nmsg_path = _latest(reports_dir, "baseline-no_nmsg_cache-*.json")

    eval_data = _read_json(eval_path)
    load_data = _read_json(load_path)
    no_multi = _read_json(no_multi_path)
    no_retry = _read_json(no_retry_path)
    no_guard = _read_json(no_guard_path)
    no_nmsg = _read_json(no_nmsg_path)

    baseline_scenario = _scenario(load_data, "cache_off")
    cache_scenario = _scenario(load_data, "cache_on_repeat_0.5")
    baseline_level = _highest(baseline_scenario)
    cache_level = _highest(cache_scenario)

    qps_off = baseline_level.get("qps") if baseline_level else None
    qps_on = cache_level.get("qps") if cache_level else None
    p95_off = baseline_level.get("latency_p95_ms") if baseline_level else None
    p95_on = cache_level.get("latency_p95_ms") if cache_level else None
    hit_rate = _pct(cache_level.get("cache_hit_rate")) if cache_level else None
    qps_gain = (
        round((qps_on - qps_off) / qps_off * 100.0, 1)
        if qps_off and qps_on is not None
        else None
    )
    p95_reduction = (
        round((p95_off - p95_on) / p95_off * 100.0, 1)
        if p95_off and p95_on is not None
        else None
    )

    current_pass = _pct(eval_data.get("pass_rate")) if isinstance(eval_data, dict) else None
    route_current = _pct(_category_rate(eval_data, "route_rule_hit"))
    route_baseline = _pct(_category_rate(no_multi, "route_rule_hit"))
    error_current = _pct(_category_rate(eval_data, "error_path"))
    error_no_retry = _pct(_category_rate(no_retry, "error_path"))
    error_no_guard = _pct(_category_rate(no_guard, "error_path"))
    avg_llm = _avg_llm_calls(eval_data)
    avg_llm_baseline = _avg_llm_calls(no_multi)

    eval_file = _rel(eval_path)
    load_file = _rel(load_path)
    no_multi_file = _rel(no_multi_path)
    no_retry_file = _rel(no_retry_path)
    no_guard_file = _rel(no_guard_path)
    no_nmsg_file = _rel(no_nmsg_path)

    missing_eval = "数据缺失：未找到 eval-*.json"
    missing_load = "数据缺失：未找到 load-*.json"
    missing_multi = "数据缺失：未找到 baseline-no_multi_agent-*.json"
    missing_retry = "数据缺失：未找到 baseline-no_retry-*.json"
    missing_guard = "数据缺失：未找到 baseline-no_guardrails-*.json"
    missing_nmsg = "数据缺失：未找到 baseline-no_nmsg_cache-*.json"

    entries = [
        _entry(
            "M-01", "eval_pass_rate", current_pass, "%",
            f"{current_pass:.1f}% 任务通过率" if current_pass is not None else "—",
            "B", eval_file, "pass_rate", "COND-02",
            "当前代码、Mock 模式；跳过 only_real_llm 任务" if eval_data else missing_eval,
        ),
        _entry(
            "M-02", "route_rule_hit_pass_rate", route_current, "%",
            _pct_display(route_current), "B", eval_file,
            "by_category.route_rule_hit.pass_rate", "COND-02",
            "当前多 Agent 路由" if route_current is not None else missing_eval,
        ),
        _entry(
            "M-03", "route_rule_hit_pass_rate_no_multi_agent", route_baseline, "%",
            _pct_display(route_baseline), "B", no_multi_file,
            "by_category.route_rule_hit.pass_rate", "COND-02",
            "强制单一默认 Profile，模拟改造前单 Agent" if route_baseline is not None else missing_multi,
        ),
        _entry(
            "M-04", "route_pass_rate_delta_pp", _delta(route_current, route_baseline), "pp",
            (
                f"{_delta(route_current, route_baseline):+.1f} 个百分点"
                if _delta(route_current, route_baseline) is not None
                else "—"
            ),
            "B", eval_file,
            "by_category.route_rule_hit.pass_rate 之差", "COND-02",
            "多 Agent 路由相对单 Agent 的路由类任务通过率差值",
        ),
        _entry(
            "M-05", "cache_hit_rate", hit_rate, "%",
            _pct_display(hit_rate), "B", load_file,
            "scenarios[cache_on_repeat_0.5].levels[concurrency=8].cache_hit_rate",
            "COND-01",
            "Mock 模式；命中率包含首次集合的池子复用" if hit_rate is not None else missing_load,
        ),
        _entry(
            "M-06", "cache_qps_gain_pct", qps_gain, "%",
            _pct_display(qps_gain), "B", load_file,
            "cache_on.qps vs cache_off.qps（8 并发）", "COND-01",
            "Mock 模式；cache-miss 注入 10ms 模拟编排耗时，绝对 QPS 不代表生产容量"
            if qps_gain is not None else missing_load,
        ),
        _entry(
            "M-07", "cache_p95_reduction_pct", p95_reduction, "%",
            _pct_display(p95_reduction), "B", load_file,
            "cache_off.p95 vs cache_on.p95（8 并发）", "COND-01",
            "Mock 模式；P95 可能受 miss 尾部影响" if p95_reduction is not None else missing_load,
        ),
        _entry(
            "M-08", "cache_off_qps", qps_off, "QPS", _qps_display(qps_off),
            "B", load_file, "scenarios[cache_off].levels[concurrency=8].qps",
            "COND-01", "Mock 模式，不能当作生产容量" if qps_off is not None else missing_load,
        ),
        _entry(
            "M-09", "cache_on_qps", qps_on, "QPS", _qps_display(qps_on),
            "B", load_file,
            "scenarios[cache_on_repeat_0.5].levels[concurrency=8].qps",
            "COND-01", "Mock 模式，不能当作生产容量" if qps_on is not None else missing_load,
        ),
        _entry(
            "M-10", "avg_llm_calls_per_task", avg_llm, "次/任务",
            _count_display(avg_llm), "B", eval_file, "llm_calls_total / executed",
            "COND-02", "Mock 不调用真实 LLM，调用次数恒为 0" if avg_llm is not None else missing_eval,
        ),
        _entry(
            "M-11", "no_multi_agent_avg_llm_calls", avg_llm_baseline, "次/任务",
            _count_display(avg_llm_baseline), "B", no_multi_file,
            "llm_calls_total / executed", "COND-02",
            "Mock 模式下与当前值相同，均不调用真实 LLM" if avg_llm_baseline is not None else missing_multi,
        ),
        _entry(
            "M-12", "tool_subset_violations", None, "次", "—", "C", "",
            "无法量化", "COND-02",
            "MockLLM 不感知 Profile 工具子集；真实 LLM 模式未运行，无法量化越权调用",
        ),
        _entry(
            "M-13", "error_path_pass_rate", error_current, "%",
            _pct_display(error_current), "B", eval_file,
            "by_category.error_path.pass_rate", "COND-02",
            "error_path 分类覆盖工具异常、LLM 超时与路由异常回退"
            if error_current is not None else missing_eval,
        ),
        _entry(
            "M-14", "no_retry_error_path_pass_rate", error_no_retry, "%",
            _pct_display(error_no_retry), "B", no_retry_file,
            "by_category.error_path.pass_rate", "COND-02",
            "Retry 默认关闭；该对照与当前配置一致" if error_no_retry is not None else missing_retry,
        ),
        _entry(
            "M-15", "no_guardrails_error_path_pass_rate", error_no_guard, "%",
            _pct_display(error_no_guard), "B", no_guard_file,
            "by_category.error_path.pass_rate", "COND-02",
            "关闭两阶段确认仅用于对照，不是推荐配置"
            if error_no_guard is not None else missing_guard,
        ),
        _entry(
            "M-16", "no_nmsg_cache_eval_pass_rate",
            _pct(no_nmsg.get("pass_rate")) if isinstance(no_nmsg, dict) else None,
            "%",
            _pct_display(_pct(no_nmsg.get("pass_rate")) if isinstance(no_nmsg, dict) else None),
            "B", no_nmsg_file, "pass_rate", "COND-02",
            "关闭缓存后，期望命中缓存的 cache_hit 任务会失败；该值是缓存相关任务通过率差异"
            if isinstance(no_nmsg, dict) else missing_nmsg,
        ),
        _entry(
            "M-17", "eval_pass_rate_no_multi_agent",
            _pct(no_multi.get("pass_rate")) if isinstance(no_multi, dict) else None,
            "%",
            _pct_display(_pct(no_multi.get("pass_rate")) if isinstance(no_multi, dict) else None),
            "B", no_multi_file, "pass_rate", "COND-02",
            "强制单一默认 Profile 后的整体任务通过率" if isinstance(no_multi, dict) else missing_multi,
        ),
        _entry(
            "M-18", "cache_off_p95_ms", p95_off, "ms",
            "—" if p95_off is None else f"{p95_off:.1f} ms",
            "B", load_file, "scenarios[cache_off].levels[concurrency=8].latency_p95_ms",
            "COND-01", "Mock 模式，不能当作生产容量" if p95_off is not None else missing_load,
        ),
        _entry(
            "M-19", "cache_on_p95_ms", p95_on, "ms",
            "—" if p95_on is None else f"{p95_on:.1f} ms",
            "B", load_file,
            "scenarios[cache_on_repeat_0.5].levels[concurrency=8].latency_p95_ms",
            "COND-01", "Mock 模式，不能当作生产容量" if p95_on is not None else missing_load,
        ),
        _entry(
            "M-20", "eval_task_count",
            eval_data.get("total") if isinstance(eval_data, dict) else None,
            "个",
            (
                f"{int(eval_data.get('total'))} 个任务"
                if isinstance(eval_data, dict) and eval_data.get("total") is not None
                else "—"
            ),
            "B", eval_file, "total", "COND-02",
            "任务集规模；其中 only_real_llm 任务在 Mock 下跳过" if isinstance(eval_data, dict) else missing_eval,
        ),
        _entry(
            "M-21", "load_total_requests",
            (
                sum(level.get("total_requests", 0) for item in load_data for level in item.get("levels", []))
                if isinstance(load_data, list)
                else None
            ),
            "次",
            (
                f"{sum(level.get('total_requests', 0) for item in load_data for level in item.get('levels', []))} 次请求"
                if isinstance(load_data, list)
                else "—"
            ),
            "B", load_file, "sum(levels[].total_requests)", "COND-01",
            "Mock 并发压测的总请求数" if isinstance(load_data, list) else missing_load,
        ),
        _entry(
            "M-22", "overall_pass_rate_delta_pp",
            _delta(current_pass, _pct(no_multi.get("pass_rate")) if isinstance(no_multi, dict) else None),
            "pp",
            (
                f"{_delta(current_pass, _pct(no_multi.get('pass_rate'))):+.1f} 个百分点"
                if _delta(current_pass, _pct(no_multi.get("pass_rate")) if isinstance(no_multi, dict) else None) is not None
                else "—"
            ),
            "B", no_multi_file, "pass_rate 差值", "COND-05",
            "多 Agent 相对单 Agent 的整体任务通过率差值" if isinstance(no_multi, dict) else missing_multi,
        ),
        _entry(
            "M-23", "cache_eval_pass_rate_delta_pp",
            _delta(current_pass, _pct(no_nmsg.get("pass_rate")) if isinstance(no_nmsg, dict) else None),
            "pp",
            (
                f"{_delta(current_pass, _pct(no_nmsg.get('pass_rate'))):+.1f} 个百分点"
                if _delta(current_pass, _pct(no_nmsg.get("pass_rate")) if isinstance(no_nmsg, dict) else None) is not None
                else "—"
            ),
            "B", no_nmsg_file, "pass_rate 差值", "COND-02",
            "开启缓存相对关闭缓存的评测通过率差值" if isinstance(no_nmsg, dict) else missing_nmsg,
        ),
    ]
    return entries


def write_index(entries: list[dict], *, out_dir: Path, version: str) -> tuple[Path, Path]:
    """写出 JSON 与人读 Markdown 索引。

    参数:
        entries: 指标条目。
        out_dir: 输出目录。
        version: 代码版本。

    返回:
        (json_path, md_path)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "code_version": version,
        "entries": entries,
    }
    json_path = out_dir / "metrics-index.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# 数字来源索引",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 代码版本：{payload['code_version']}",
        "",
        "| ID | 指标 | 展示文本 | 来源 | 条件 | 注意事项 |",
        "|---|---|---|---|---|---|",
    ]
    for entry in entries:
        caveat = entry["caveat"].replace("|", "｜")
        source = entry["source_file"] or entry["source_type"]
        lines.append(
            f"| {entry['id']} | {entry['metric']} | {entry['display']} | "
            f"{source} | {entry['condition']} | {caveat} |"
        )
    md_path = out_dir / "metrics-index.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv=None) -> int:
    """命令行入口：读取 reports/ 并生成指标索引。

    参数:
        argv: 可选参数列表（默认读取 sys.argv）。

    返回:
        进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description="汇总评测/压测报告为指标索引")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS), help="报告目录")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="索引输出目录")
    parser.add_argument("--code-version", default=None, help="覆盖代码版本锚点")
    args = parser.parse_args(argv)

    reports_dir = Path(args.reports_dir)
    version = args.code_version or code_version()
    entries = build_entries(reports_dir, version)
    json_path, md_path = write_index(entries, out_dir=Path(args.out), version=version)
    print(f"[metrics] {json_path}")
    print(f"[metrics] {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

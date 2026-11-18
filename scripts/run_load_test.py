#!/usr/bin/env python
"""并发压测 CLI：执行场景并生成 reports/load-<时间>-<mode> 报告。

示例:
    python scripts/run_load_test.py --scenarios cache_off cache_on_repeat_0.5 --mode mock

说明:
    默认 in-process 压 ASGI app，不启动 uvicorn；不引入任何压测框架依赖。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system.eval.load_runner import run_load  # noqa: E402
from agent_system.eval.load_scenarios import (  # noqa: E402
    DEFAULT_SCENARIO_NAMES,
    select_scenarios,
)


def main(argv=None) -> int:
    """解析参数、执行压测并打印简要结果。

    参数:
        argv: 可选参数列表（默认读取 sys.argv）。

    返回:
        进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description="并发压测器（in-process ASGI）")
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=list(DEFAULT_SCENARIO_NAMES),
        help="场景名列表；传 all 启用全部内置场景",
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "real"),
        default="mock",
        help="运行模式：mock（默认，无 LLM 网络往返）/ real（需 API Key）",
    )
    parser.add_argument(
        "--concurrency",
        nargs="+",
        type=int,
        default=None,
        help="覆盖场景默认并发度，例如 --concurrency 1 4 8",
    )
    parser.add_argument(
        "--requests-per-worker",
        type=int,
        default=25,
        help="每个 worker 的请求数（默认 25）",
    )
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    parser.add_argument(
        "--out",
        default=str(ROOT / "reports"),
        help="报告输出目录（默认 reports/）",
    )
    args = parser.parse_args(argv)

    scenarios = select_scenarios(args.scenarios)
    if args.concurrency:
        levels = tuple(sorted({level for level in args.concurrency if level > 0}))
        if not levels:
            parser.error("--concurrency 必须包含正整数")
        scenarios = [
            replace(scenario, concurrency_levels=levels) for scenario in scenarios
        ]

    def _progress(text: str) -> None:
        """压测进度直接打印到终端，便于观察长任务。"""
        print(text, flush=True)

    results = run_load(
        scenarios,
        mode=args.mode,
        seed=args.seed,
        requests_per_worker=args.requests_per_worker,
        out_dir=args.out,
        progress=_progress,
    )

    summary = []
    for result in results:
        level = result.highest_level()
        summary.append(
            {
                "scenario": result.scenario.name,
                "aborted": result.aborted,
                "concurrency": level.concurrency if level else None,
                "qps": level.qps if level else 0.0,
                "p95_ms": level.latency_p95_ms if level else 0.0,
                "cache_hit_rate": level.cache_hit_rate if level else 0.0,
                "error_rate": level.error_rate if level else 0.0,
            }
        )

    print(
        json.dumps(
            {"mode": args.mode, "seed": args.seed, "scenarios": summary},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告已写入: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

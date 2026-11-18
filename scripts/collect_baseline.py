#!/usr/bin/env python
"""特性开关对照采集：用当前代码关闭某模块后跑一次步骤 1 评测。

为什么复用步骤 1 的 runner 而不是新写一套:
    控制变量的关键是“同一套执行逻辑”。本脚本只改变环境变量或调用参数，
    runner、任务集、判定口径完全不变，这样产出的对照数据才可比较。

为什么全部用特性开关/运行时替换，而不是改业务代码:
    这些开关本身就是为“可评测、可降级”设计的；改代码会让对照失去意义，
    也会破坏既有 385 项测试。

no_guardrails 的定位:
    仅用于量化“关闭两阶段确认”这一极端对照，用于说明安全机制的代价；
    它不是推荐配置，报告中必须明确标注。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_system import app as app_module  # noqa: E402
from agent_system.config import get_settings  # noqa: E402
from agent_system.eval import runner as runner_module  # noqa: E402
from agent_system.eval.runner import _EvalEnvironment, run  # noqa: E402

DEFAULT_TASKS = ROOT / "evals" / "tasks.jsonl"
DEFAULT_OUT = ROOT / "reports"

# variant -> 中文说明；同时作为 argparse 的 choices，未知值会被列出
VARIANTS = {
    "no_multi_agent": "无多 Agent：强制所有请求使用同一个默认 Profile（模拟改造前单 Agent）",
    "no_nmsg_cache": "无归一化消息缓存：NMSG_CACHE_ENABLED=false",
    "no_retry": "无 Retry：RETRY_ENABLED=false",
    "no_router_llm": "无 LLM 路由：ROUTER_STRATEGY=rule_only",
    "no_guardrails": "关闭两阶段确认：CONFIRMATION_ENABLED=false（仅对照，不是推荐配置）",
}


def _variant_env(variant: str) -> dict[str, str]:
    """返回该变体需要设置的环境变量。

    参数:
        variant: 变体名。

    返回:
        环境变量字典；无关变体返回空字典。
    """
    if variant == "no_nmsg_cache":
        return {"NMSG_CACHE_ENABLED": "false"}
    if variant == "no_retry":
        return {"RETRY_ENABLED": "false"}
    if variant == "no_router_llm":
        return {"ROUTER_STRATEGY": "rule_only"}
    if variant == "no_guardrails":
        return {"CONFIRMATION_ENABLED": "false"}
    return {}


def _forced_select_profile(session_id: str, message: str):
    """强制使用默认单一 Profile，模拟改造前的单 Agent 行为。

    参数:
        session_id: 会话 ID（本实现不使用）。
        message: 用户消息（本实现不使用）。

    返回:
        (profile, None, "no_multi_agent")；decision 为 None 表示非 Router 决策。

    说明:
        runner 通过 `/chat` 执行，无法直接传 `profile=None`；这里用“始终选择
        默认 Profile”达到等价的单 Agent 效果，并让 route_selected 明确标记原因。
    """
    from agent_system.harness.agent_profile import get_profile_registry

    registry = get_profile_registry()
    profile = registry.get(get_settings().router_default_profile)
    if profile is None:
        profiles = registry.list()
        if not profiles:
            raise RuntimeError("ProfileRegistry 为空，无法模拟单 Agent")
        profile = profiles[0]
    # 与正式 selection 保持一致地发出 route_selected，避免“事件缺失”造成伪差异
    from agent_system.obs.events import log_event

    log_event(
        "route_selected",
        agent=profile.name,
        display_name=profile.display_name,
        strategy="fallback",
        reason="no_multi_agent",
        confidence=0.0,
        latency_ms=0.0,
    )
    return profile, None, "no_multi_agent"


def _make_variant_environment_class(variant: str):
    """构造一个在进入隔离环境后覆盖缓存开关的子类。

    参数:
        variant: 变体名。

    返回:
        _EvalEnvironment 的子类。

    说明:
        步骤 1 的 Mock 环境会强制开启缓存（为了确定性覆盖缓存任务）；
        `no_nmsg_cache` 变体需要在其 `__enter__` 之后把开关改回 false，
        否则环境变量会被覆盖，对照就不成立。
    """

    class _VariantEnvironment(_EvalEnvironment):
        """在标准隔离环境基础上应用变体覆盖。"""

        def __enter__(self):
            """进入隔离环境并应用本变体的额外覆盖。"""
            env = super().__enter__()
            if variant == "no_nmsg_cache":
                app_module.settings = replace(
                    app_module.settings, nmsg_cache_enabled=False
                )
            return env

    return _VariantEnvironment


@contextmanager
def apply_variant(variant: str):
    """临时应用变体的环境变量与运行时替换，退出时完整恢复。

    参数:
        variant: 变体名。

    说明:
        所有替换都只作用于本进程；测试或脚本结束后 business 代码与全局单例恢复。
    """
    saved_env: dict[str, str | None] = {}
    overrides = _variant_env(variant)
    for key, value in overrides.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value
    get_settings.cache_clear()

    saved_select = app_module.select_profile
    saved_env_class = runner_module._EvalEnvironment

    if variant == "no_multi_agent":
        app_module.select_profile = _forced_select_profile
    elif variant == "no_nmsg_cache":
        runner_module._EvalEnvironment = _make_variant_environment_class(variant)

    try:
        yield
    finally:
        app_module.select_profile = saved_select
        runner_module._EvalEnvironment = saved_env_class
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


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
    except Exception:  # noqa: BLE001 - 版本锚点不可用不应阻塞采集
        return "unknown"


def _config_snapshot(variant: str, mode: str) -> dict:
    """构造本次采集的实际配置快照。

    参数:
        variant: 变体名。
        mode: mock / real。

    返回:
        含请求配置、生效配置、代码版本与注意事项的字典。
    """
    settings = get_settings()
    # Mock 环境会为确定性缓存任务强制开启缓存；no_nmsg_cache 由子类显式关闭
    nmsg_effective = False if variant == "no_nmsg_cache" else (
        True if mode == "mock" else settings.nmsg_cache_enabled
    )
    notes = []
    if variant == "no_guardrails":
        notes.append("关闭两阶段确认仅用于量化对照，不是推荐配置")
    if variant == "no_multi_agent":
        notes.append("MockLLM 不感知 Profile 工具子集，工具越权差异无法在此模式下量化")
    if mode == "mock":
        notes.append("Mock 模式不调用真实 LLM，Token 成本与 LLM 调用量恒为 0")

    return {
        "variant": variant,
        "variant_description": VARIANTS[variant],
        "requested_env": _variant_env(variant),
        "effective": {
            "mode": mode,
            "llm_backend": "MockLLM" if mode == "mock" else "LLMClient",
            "nmsg_cache_enabled": nmsg_effective,
            "retry_enabled": bool(settings.retry_enabled),
            "router_strategy": settings.router_strategy,
            "confirmation_enabled": bool(settings.confirmation_enabled),
        },
        "code_version": code_version(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "notes": notes,
    }


def collect(
    variant: str,
    *,
    tasks_path: str | Path = DEFAULT_TASKS,
    mode: str = "mock",
    out_dir: str | Path = DEFAULT_OUT,
) -> Path:
    """执行一个变体并把 EvalReport + 配置快照写入 JSON。

    参数:
        variant: 变体名。
        tasks_path: 任务集路径。
        mode: mock / real。
        out_dir: 输出目录。

    返回:
        baseline JSON 路径。

    异常:
        ValueError: 未知变体。
        RuntimeError: mode=real 但无 API Key（由 runner 抛出）。
    """
    if variant not in VARIANTS:
        raise ValueError(
            f"未知变体: {variant!r}；可选值: {', '.join(VARIANTS)}"
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    with apply_variant(variant):
        snapshot = _config_snapshot(variant, mode)
        # runner 自己会写 eval-* 报告；这里给临时目录，避免污染正式评测产物
        # 连续跑多个变体会触发进程内限流（120 次/分钟），这里临时关闭并清空计数；
        # 否则后续变体会得到 429，对照数据全部失真。
        from agent_system.eval.load_runner import (
            _disable_rate_limit,
            _restore_rate_limit,
        )

        rate_limits = _disable_rate_limit()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                report = run(tasks_path, mode=mode, reports_dir=tmp)
        finally:
            _restore_rate_limit(rate_limits)

    payload = asdict(report)
    payload["config_snapshot"] = snapshot
    path = out_path / f"baseline-{variant}-{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main(argv=None) -> int:
    """命令行入口：按变体采集对照评测。

    参数:
        argv: 可选参数列表（默认读取 sys.argv）。

    返回:
        进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description="特性开关对照采集（复用步骤 1 runner）")
    parser.add_argument(
        "--variant",
        nargs="+",
        choices=list(VARIANTS),
        default=["no_multi_agent", "no_nmsg_cache"],
        help="对照变体；默认 no_multi_agent 与 no_nmsg_cache",
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "real"),
        default="mock",
        help="评测模式（默认 mock；real 需 API Key）",
    )
    parser.add_argument("--tasks", default=str(DEFAULT_TASKS), help="任务集 JSONL 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    args = parser.parse_args(argv)

    for variant in args.variant:
        path = collect(variant, tasks_path=args.tasks, mode=args.mode, out_dir=args.out)
        print(f"[baseline] {variant} -> {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

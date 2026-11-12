"""本地评估 runner：读取固定任务集，跑一轮 /chat 并用 code evaluator 打分。

本模块属于评估层，不依赖真实 LLM：默认使用 Mock 模式（未配置 API Key 时）。
它只做本地评估，不自动上传数据集；若 Langfuse 已启用，会额外把分数上报到 trace。
"""

import argparse
import asyncio
import json
import logging
import tempfile
from pathlib import Path

import httpx

from agent_system.config import PROJECT_ROOT
from agent_system.eval.code_evaluators import evaluate_trajectory, report_scores
from agent_system.app import app
from agent_system.obs.langfuse_client import get_langfuse, is_enabled
from agent_system.obs.langfuse_mapping import trace_id_for
from agent_system.obs.logging_config import JSONFormatter, get_logger

_DEFAULT_TASKS = PROJECT_ROOT / "evals" / "tasks.jsonl"


class _ListHandler(logging.Handler):
    """把应用日志记录收集到内存，供 runner 还原每次请求的事件轨迹。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        """收集单条日志记录（不做格式化，解析时再统一 format）。"""
        self.records.append(record)


def load_tasks(path: Path) -> list[dict]:
    """读取 JSONL 任务集。

    参数:
        path: tasks.jsonl 路径，每行至少包含 input 字段。

    返回:
        任务字典列表。

    异常:
        FileNotFoundError: 任务文件不存在。
        ValueError: 某一行不是合法 JSON 对象。
    """
    tasks = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"任务文件第 {line_no} 行不是合法 JSON: {exc}") from exc
        if not isinstance(task, dict) or "input" not in task:
            raise ValueError(f"任务文件第 {line_no} 行缺少 input 字段")
        tasks.append(task)
    return tasks


def _parse_records(handler: _ListHandler, request_id: str) -> list[dict]:
    """从收集到的日志中筛出指定请求的事件轨迹。"""
    events = []
    for record in handler.records:
        try:
            payload = json.loads(handler.format(record))
        except Exception:  # noqa: BLE001 - 单条日志解析失败不影响整体评估
            continue
        if payload.get("request_id") == request_id:
            events.append(payload)
    return events


async def _run_tasks(tasks: list[dict]) -> dict:
    """顺序执行任务并汇总评分。

    参数:
        tasks: 任务字典列表。

    返回:
        汇总字典，包含任务数、平均工具成功率、错误率与逐任务明细。
    """
    handler = _ListHandler()
    handler.setFormatter(JSONFormatter("eval-runner"))
    logger = get_logger()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    results = []
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://eval") as client:
            for index, task in enumerate(tasks):
                request_id = f"eval-{index:04d}"
                response = await client.post(
                    "/chat",
                    json={
                        "session_id": f"eval-session-{index}",
                        "message": str(task["input"]),
                    },
                    headers={"X-Request-ID": request_id},
                )
                events = _parse_records(handler, request_id)
                scores = evaluate_trajectory(events)
                # 仅在启用时上报；NoOp 模式不会产生任何网络调用
                if is_enabled():
                    report_scores(get_langfuse(), trace_id_for(request_id), scores)
                results.append(
                    {
                        "input": task["input"],
                        "expected_tool": task.get("expected_tool"),
                        "status": response.status_code,
                        "scores": scores,
                    }
                )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    tool_values = [
        score["value"]
        for result in results
        for score in result["scores"]
        if score["name"] == "tool_success_rate"
    ]
    error_values = [
        score["value"]
        for result in results
        for score in result["scores"]
        if score["name"] == "error_rate"
    ]

    return {
        "tasks": len(results),
        "tool_success_rate": (
            round(sum(tool_values) / len(tool_values), 4) if tool_values else None
        ),
        "error_rate": round(sum(error_values) / len(error_values), 4) if error_values else 0.0,
        "results": results,
    }


def main(argv=None) -> int:
    """命令行入口：读取任务集、执行评估并打印汇总 JSON。

    参数:
        argv: 可选参数列表（默认读取 sys.argv）。

    返回:
        进程退出码，0 表示成功。
    """
    parser = argparse.ArgumentParser(description="本地 code evaluator runner")
    parser.add_argument("--tasks", default=str(_DEFAULT_TASKS), help="任务集 JSONL 路径")
    args = parser.parse_args(argv)

    tasks = load_tasks(Path(args.tasks))

    # 使用临时 SQLite 会话库，避免往项目根目录写运行数据
    from agent_system import app as app_module
    from agent_system.memory import SessionStore

    original_store = app_module.store
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SessionStore(db_path=str(Path(tmpdir) / "sessions.db"))
        app_module.store = store
        try:
            summary = asyncio.run(_run_tasks(tasks))
        finally:
            # 先恢复 store 并关闭 SQLite 连接，否则 Windows 下临时目录无法删除
            app_module.store = original_store
            connection = getattr(store, "_conn", None)
            if connection is not None:
                connection.close()

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

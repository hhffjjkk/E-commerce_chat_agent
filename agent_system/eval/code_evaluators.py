"""确定性 code evaluator 示例。

本模块属于评估层：只消费一次请求的结构化事件轨迹（request_start → request_end），
用确定性规则计算分数，不调用 LLM、不访问网络。分数可作为 Langfuse score 上报。
"""

# 评分名常量，避免调用方与上报层写错字符串
TOOL_SUCCESS_RATE = "tool_success_rate"
ERROR_RATE = "error_rate"


def tool_success_rate(events) -> dict | None:
    """计算工具调用成功率。

    参数:
        events: 事件字典列表（含 event / ok 等字段）。

    返回:
        {"name": "tool_success_rate", "value": 0-1, "data_type": "NUMERIC"}；
        若轨迹中没有 tool_call，返回 None（无数据不评分）。
    """
    calls = [event for event in events if event.get("event") == "tool_call"]
    if not calls:
        return None
    ok_count = sum(1 for event in calls if event.get("ok") is True)
    return {
        "name": TOOL_SUCCESS_RATE,
        "value": round(ok_count / len(calls), 4),
        "data_type": "NUMERIC",
    }


def error_rate(events) -> dict:
    """计算轨迹错误率（布尔型：是否出现错误事件）。

    参数:
        events: 事件字典列表。

    返回:
        {"name": "error_rate", "value": 0 或 1, "data_type": "BOOLEAN"}。
    """
    failed = any(
        event.get("event") in {"error_raised", "request_error"} for event in events
    )
    return {
        "name": ERROR_RATE,
        "value": 1 if failed else 0,
        "data_type": "BOOLEAN",
    }


def evaluate_trajectory(events) -> list[dict]:
    """对一条完整轨迹计算全部可用分数。

    参数:
        events: 事件字典列表。

    返回:
        分数列表；无工具调用时只包含 error_rate。
    """
    scores = []
    tool_score = tool_success_rate(events)
    if tool_score is not None:
        scores.append(tool_score)
    scores.append(error_rate(events))
    return scores


def report_scores(client, trace_id: str, scores) -> int:
    """把分数上报到 Langfuse trace（可选）。

    参数:
        client: Langfuse 客户端或 NoOpLangfuse。
        trace_id: 目标 trace id。
        scores: evaluate_trajectory 返回的分数列表。

    返回:
        实际尝试上报的分数条数；客户端不支持评分 API 时返回 0。

    说明:
        v3 SDK 使用 `create_score`；兼容旧版 `score` 命名。NoOpLangfuse 两个方法
        都是 no-op，因此不需要到处判断 enabled。
    """
    creator = getattr(client, "create_score", None)
    if not callable(creator):
        creator = getattr(client, "score", None)
    if not callable(creator):
        return 0
    for score in scores:
        creator(
            trace_id=trace_id,
            name=score["name"],
            value=score["value"],
            data_type=score["data_type"],
        )
    return len(scores)

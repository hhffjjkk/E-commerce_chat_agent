"""确定性 code evaluator 示例。

本模块属于评估层：只消费一次请求的结构化事件轨迹（request_start → request_end），
用确定性规则计算分数，不调用 LLM、不访问网络。分数可作为 Langfuse score 上报。
"""

# 评分名常量，避免调用方与上报层写错字符串
TOOL_SUCCESS_RATE = "tool_success_rate"
ERROR_RATE = "error_rate"
ROUTE_MATCH = "route_match"
CONTAINS_KEYWORDS = "contains_keywords"
NO_WRITE_TOOLS = "no_write_tools"
CACHE_HIT_EXPECTED = "cache_hit_expected"


def _bool_score(name: str, passed: bool) -> dict:
    """构造统一的布尔型评分字典。

    参数:
        name: 评分名。
        passed: 是否通过。

    返回:
        {"name", "value": 1/0, "data_type": "BOOLEAN"}；阶段六报告按 value 判定。
    """
    return {"name": name, "value": 1 if passed else 0, "data_type": "BOOLEAN"}


def route_match(actual_agent: str | None, expected_agent: str | None) -> dict | None:
    """校验实际路由结果是否等于期望 Profile。

    输入:
        actual_agent: `route_selected.agent`（可能为 None）。
        expected_agent: `ExpectSpec.route_agent`。

    返回:
        评分字典；expected_agent 为 None 时返回 None（不适用）。

    说明:
        只比较 Profile name，不比较 strategy/confidence：
        同一 Profile 由规则还是 LLM 命中不属于本 evaluator 的职责。
    """
    if expected_agent is None:
        return None
    return _bool_score(ROUTE_MATCH, actual_agent == expected_agent)


def contains_keywords(reply: str | None, keywords) -> dict | None:
    """校验回复是否包含全部期望关键词。

    输入:
        reply: 最终 assistant 回复文本（可能为 None）。
        keywords: ExpectSpec.contains（字符串序列）。

    返回:
        评分字典；keywords 为空时返回 None（不适用）。

    说明:
        采用“全部包含”语义：任一关键词缺失即失败；
        不做大小写或空白归一化，避免评测口径随实现细节漂移。
    """
    if not keywords:
        return None
    text = reply or ""
    return _bool_score(CONTAINS_KEYWORDS, all(keyword in text for keyword in keywords))


def no_write_tools(
    tools_used,
    registry_snapshot: dict | None = None,
    *,
    expected: bool = True,
) -> dict | None:
    """校验本次任务没有触发任何带 write 标签的工具。

    输入:
        tools_used: 本次任务调用过的工具名列表。
        registry_snapshot: 工具名 → ToolSpec 的映射；None 时读取全局 Registry。
        expected: ExpectSpec.no_write_tools；False 时不适用。

    返回:
        评分字典；expected=False 时返回 None（不适用）。

    说明:
        只依据注册表标签判定，不维护第二份写工具名单；
        未注册的工具名视为非写工具（它本身会在别处触发 tool_failed）。
    """
    if not expected:
        return None
    if registry_snapshot is None:
        from agent_system.harness.tool_registry import get_registry

        registry_snapshot = get_registry().snapshot()
    used = list(tools_used or [])
    has_write = any(
        name in registry_snapshot and "write" in registry_snapshot[name].tags
        for name in used
    )
    return _bool_score(NO_WRITE_TOOLS, not has_write)


def cache_hit_expected(actual_hit: bool, expected_hit: bool | None) -> dict | None:
    """校验缓存命中状态是否符合期望。

    输入:
        actual_hit: 任务内是否出现过 `cache_hit` 事件。
        expected_hit: ExpectSpec.cache_hit；None 时不适用。

    返回:
        评分字典；expected_hit 为 None 时返回 None（不适用）。
    """
    if expected_hit is None:
        return None
    return _bool_score(CACHE_HIT_EXPECTED, bool(actual_hit) == bool(expected_hit))


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

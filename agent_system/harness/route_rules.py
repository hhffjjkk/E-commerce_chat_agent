# agent_system/harness/route_rules.py
# 规则路由表：关键词 → 子 Agent 的静态映射与加权评分。
# 规则路由零成本、零延迟、可解释，作为默认策略的第一层。

from __future__ import annotations

from dataclasses import dataclass

PROFILE_NAMES = ("pre_sales", "order", "after_sales")


@dataclass(frozen=True)
class KeywordRule:
    """一条关键词规则。

    字段说明：
        profile: 目标 Profile name。
        keywords: 中文关键词元组，命中任一即加分。
        weight: 单次命中权重。
        description: 规则用途说明（中文，便于维护）。
    """

    profile: str
    keywords: tuple[str, ...]
    weight: float = 1.0
    description: str = ""


# 关键词按业务域分组：
# - 售前：商品属性/价格/选购类，命中后无需触达订单或写操作；
# - 订单：订单号/物流/配送类，只读查询；
# - 售后：退款/取消/投诉类，涉及写操作与转人工，需要独立 Agent。
# 所有关键词在不同 Profile 间必须互斥，构造时由 validate_rule_table 检查。
RULE_TABLE: tuple[KeywordRule, ...] = (
    KeywordRule(
        profile="pre_sales",
        keywords=(
            "商品",
            "价格",
            "多少钱",
            "库存",
            "有货",
            "规格",
            "尺寸",
            "颜色",
            "功能",
            "推荐",
            "哪个好",
            "适合",
            "参数",
        ),
        description="商品信息、价格、库存与选购咨询",
    ),
    KeywordRule(
        profile="order",
        keywords=(
            "订单",
            "物流",
            "快递",
            "单号",
            "发货",
            "到哪了",
            "签收",
            "配送",
            "运单",
        ),
        description="订单状态与物流轨迹查询",
    ),
    KeywordRule(
        profile="after_sales",
        keywords=(
            "退款",
            "退货",
            "取消",
            "售后",
            "换货",
            "补偿",
            "投诉",
            "申请退",
        ),
        description="退款、取消、退换货与售后处理",
    ),
)


def validate_rule_table(table: tuple[KeywordRule, ...] = RULE_TABLE) -> None:
    """校验规则表覆盖完整且关键词互斥。

    参数:
        table: 待校验规则表；默认校验模块内置表。

    异常:
        RuntimeError: 非法 Profile、关键词跨 Profile 重复、某 Profile 无规则。

    说明:
        规则表是设计期配置；错误属于代码缺陷而非用户输入，因此用 RuntimeError。
    """
    seen: dict[str, str] = {}
    covered = set()
    for rule in table:
        if rule.profile not in PROFILE_NAMES:
            raise RuntimeError(f"规则引用了未知 Profile: {rule.profile}")
        covered.add(rule.profile)
        for keyword in rule.keywords:
            if keyword in seen:
                raise RuntimeError(
                    f"关键词重复: {keyword!r} 同时出现在 {seen[keyword]} 与 {rule.profile}"
                )
            seen[keyword] = rule.profile

    missing = set(PROFILE_NAMES) - covered
    if missing:
        raise RuntimeError(f"以下 Profile 缺少规则: {sorted(missing)}")


# 模块导入即校验：让规则表错误在启动/导包阶段立刻暴露，而不是等到线上请求
validate_rule_table()


def score_message(message: str) -> dict[str, float]:
    """按关键词给每个 Profile 打分。

    参数:
        message: 用户消息。

    返回:
        {profile: score}，未命中为 0；每个内置 Profile 都会出现。

    口径:
        每命中一个关键词加该规则的 weight；同一关键词只计一次。
    """
    scores = {name: 0.0 for name in PROFILE_NAMES}
    for rule in RULE_TABLE:
        for keyword in rule.keywords:
            if keyword in message:
                scores[rule.profile] += rule.weight
    return scores


def best_rule(message: str) -> tuple[str | None, float]:
    """返回得分最高的 Profile 与归一化置信度。

    参数:
        message: 用户消息。

    返回:
        (profile, confidence)；全部为 0 或最高分并列时返回 (None, 0.0)。

    说明:
        置信度使用 `score / (score + 1)`：单调递增且恒小于 1，
        避免“多命中几个关键词就给 1.0”的虚假高置信。
    """
    scores = score_message(message)
    highest = max(scores.values())
    if highest <= 0:
        return None, 0.0

    winners = [name for name, score in scores.items() if score == highest]
    if len(winners) > 1:
        # 并列说明规则无法区分业务域，交给 LLM 或默认 Profile 兜底
        return None, 0.0

    profile = winners[0]
    confidence = min(1.0, highest / (highest + 1))
    return profile, confidence

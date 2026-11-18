"""并发压测的消息池与场景定义。

职责边界:
    - `evals/tasks.jsonl` 用于正确性评测（有明确期望、覆盖错误路径）；
    - `evals/load_messages.jsonl` 用于并发压测（只关心吞吐/延迟/缓存收益，无期望字段）。

为什么压测消息池不含写操作消息:
    并发压测聚焦读路径与归一化消息缓存；退款/取消会触发服务端两阶段确认，
    并发下的 pending 状态机正确性已由步骤 1 的 pending_flow 任务覆盖。
    把写操作放进压测会引入确认失败、状态竞争与数据变更，干扰性能结论。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

# 三个内置 Profile；消息池按此分桶
PROFILE_NAMES = ("pre_sales", "order", "after_sales")

# 压测消息池禁止出现的写操作关键词（退款/取消/换货/补偿都会进入售后写入或确认流程）
WRITE_KEYWORDS = ("退款", "退货", "取消", "换货", "补偿", "申请退")

# 允许的压测路径
TARGET_PATHS = ("/chat", "/chat/stream")

# Mock 模式下 cache-miss 请求的“模拟编排耗时”（毫秒）。
# 为什么需要它：MockLLM 是纯本地规则引擎，一次回复只要几十微秒，
# 而 in-process 的 ASGI/线程池开销约 1ms，缓存节省会被完全淹没，
# 导致“缓存收益”在 Mock 下不可测。这里用固定小延迟代表真实 LLM 的
# 编排/推理成本；它只作用于压测进程，不改变生产行为，也不代表真实模型延迟。
# 报告中的绝对 QPS 因此只用于相对比较，不能当作生产容量。
MOCK_LLM_ORCHESTRATION_MS = 10.0


@dataclass(frozen=True)
class LoadMessage:
    """压测消息池中的一条消息。"""

    id: str
    profile_hint: str
    message: str
    repeat_friendly: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "LoadMessage":
        """从 JSONL 单行构造消息并校验字段。

        参数:
            raw: 反序列化后的字典。

        返回:
            LoadMessage。

        异常:
            ValueError: id/message 缺失、profile_hint 非法或类型不正确。
        """
        if not isinstance(raw, dict):
            raise ValueError("压测消息必须是 JSON 对象")
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("压测消息缺少非空 id")
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"压测消息 {item_id} 的 message 必须是非空字符串")
        profile_hint = raw.get("profile_hint")
        if profile_hint not in PROFILE_NAMES:
            raise ValueError(
                f"压测消息 {item_id} 的 profile_hint 非法: {profile_hint!r}"
            )
        repeat_friendly = raw.get("repeat_friendly", False)
        if not isinstance(repeat_friendly, bool):
            raise ValueError(f"压测消息 {item_id} 的 repeat_friendly 必须是布尔值")
        return cls(
            id=item_id,
            profile_hint=profile_hint,
            message=message,
            repeat_friendly=repeat_friendly,
        )


def load_messages(path: Path | str) -> list[LoadMessage]:
    """读取并校验压测消息池。

    参数:
        path: load_messages.jsonl 路径。

    返回:
        按文件顺序排列的消息列表。

    异常:
        ValueError: JSON 非法、id 重复或包含写操作关键词。
    """
    messages = []
    seen: set[str] = set()
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"消息池第 {line_no} 行不是合法 JSON: {exc}") from exc
        message = LoadMessage.from_dict(raw)
        if message.id in seen:
            raise ValueError(f"压测消息 id 重复: {message.id}")
        if any(keyword in message.message for keyword in WRITE_KEYWORDS):
            raise ValueError(
                f"压测消息 {message.id} 含写操作关键词，不允许进入压测池"
            )
        seen.add(message.id)
        messages.append(message)
    return messages


@dataclass(frozen=True)
class LoadScenario:
    """一个压测场景。

    name: 稳定标识，如 "cache_off" / "cache_on_repeat_0.5"。
    description: 中文描述，进入报告。
    cache_enabled: 是否启用归一化消息缓存。
    repeat_ratio: 重复消息比例（0.0–1.0）。
    target_path: "/chat" 或 "/chat/stream"。
    concurrency_levels: 并发度列表，必须严格递增。
    requests_per_level: 每个并发度的请求总数；0 表示按 concurrency × 25 自动计算。
    seed: 随机种子，决定消息选择与顺序。
    """

    name: str
    description: str
    cache_enabled: bool
    repeat_ratio: float
    target_path: str
    concurrency_levels: tuple[int, ...] = (1, 4, 8)
    requests_per_level: int = 0
    seed: int = 42

    def validate(self) -> None:
        """校验场景参数；非法时抛 ValueError。

        说明:
            场景是设计期配置，错误应该尽早暴露，而不是压测跑到一半才发现。
        """
        if not self.name:
            raise ValueError("场景 name 不能为空")
        if not 0.0 <= self.repeat_ratio <= 1.0:
            raise ValueError(f"场景 {self.name} 的 repeat_ratio 必须在 0~1 之间")
        if self.target_path not in TARGET_PATHS:
            raise ValueError(f"场景 {self.name} 的 target_path 非法: {self.target_path}")
        if not self.concurrency_levels:
            raise ValueError(f"场景 {self.name} 的并发度列表不能为空")
        if any(level <= 0 for level in self.concurrency_levels):
            raise ValueError(f"场景 {self.name} 的并发度必须为正整数")
        if list(self.concurrency_levels) != sorted(set(self.concurrency_levels)):
            raise ValueError(f"场景 {self.name} 的并发度必须严格递增且不重复")
        if self.requests_per_level < 0:
            raise ValueError(f"场景 {self.name} 的 requests_per_level 不能为负")


# 默认并发度上限 8：in-process 模式下 ASGITransport 共享同一事件循环，
# 超过 8 后延迟主要由事件循环调度而非业务逻辑决定，继续增加并发度对结论无增益。
SCENARIO_CACHE_OFF = LoadScenario(
    name="cache_off",
    description="无缓存基线：每条消息只发送一次",
    cache_enabled=False,
    repeat_ratio=0.0,
    target_path="/chat",
)

SCENARIO_CACHE_ON_REPEAT_05 = LoadScenario(
    name="cache_on_repeat_0.5",
    description="启用缓存，一半请求来自可重复消息",
    cache_enabled=True,
    repeat_ratio=0.5,
    target_path="/chat",
)

SCENARIO_CACHE_ON_REPEAT_09 = LoadScenario(
    name="cache_on_repeat_0.9",
    description="启用缓存，高重复比例，接近纯内存响应上限",
    cache_enabled=True,
    repeat_ratio=0.9,
    target_path="/chat",
)

# 流式场景默认不跑：ASGITransport 对流式响应的分块与消费有额外开销，
# 与 /chat 直接对比容易被误读为“业务变慢”。需要时通过 --scenarios 显式启用。
SCENARIO_CACHE_ON_STREAM_REPEAT_05 = LoadScenario(
    name="cache_on_stream_repeat_0.5",
    description="流式路径 + 缓存，用于观察首字节延迟（TTFB）",
    cache_enabled=True,
    repeat_ratio=0.5,
    target_path="/chat/stream",
)

BUILTIN_SCENARIOS: tuple[LoadScenario, ...] = (
    SCENARIO_CACHE_OFF,
    SCENARIO_CACHE_ON_REPEAT_05,
    SCENARIO_CACHE_ON_REPEAT_09,
    SCENARIO_CACHE_ON_STREAM_REPEAT_05,
)

# CLI 默认只跑两个场景：基线 + 50% 重复，保证 CI 友好
DEFAULT_SCENARIO_NAMES: tuple[str, ...] = ("cache_off", "cache_on_repeat_0.5")

_BY_NAME = {scenario.name: scenario for scenario in BUILTIN_SCENARIOS}


def scenario_by_name(name: str) -> LoadScenario:
    """按名称取内置场景；未知名称抛 ValueError。"""
    scenario = _BY_NAME.get(name)
    if scenario is None:
        raise ValueError(
            f"未知压测场景: {name!r}；可选值: {', '.join(_BY_NAME)} 或 all"
        )
    return scenario


def select_scenarios(names) -> list[LoadScenario]:
    """把 CLI 的场景名列表解析为场景对象。

    参数:
        names: 场景名序列；包含 "all" 时返回全部内置场景。

    返回:
        按入参顺序排列的场景列表（去重前保持首次出现顺序）。
    """
    selected: list[LoadScenario] = []
    seen: set[str] = set()
    for name in names:
        if name == "all":
            for scenario in BUILTIN_SCENARIOS:
                if scenario.name not in seen:
                    seen.add(scenario.name)
                    selected.append(scenario)
            continue
        scenario = scenario_by_name(name)
        if scenario.name not in seen:
            seen.add(scenario.name)
            selected.append(scenario)
    return selected


def resolve_requests(
    scenario: LoadScenario, concurrency: int, requests_per_worker: int = 25
) -> int:
    """计算某个并发度下的请求总数。

    参数:
        scenario: 压测场景。
        concurrency: 当前并发度。
        requests_per_worker: 每个 worker 的请求数（默认 25）。

    返回:
        场景显式配置了 requests_per_level 时优先使用；否则 concurrency × 每 worker 数。
    """
    if scenario.requests_per_level > 0:
        return scenario.requests_per_level
    return max(1, concurrency * max(1, requests_per_worker))


def build_request_sequence(
    messages: list[LoadMessage],
    *,
    count: int,
    repeat_ratio: float,
    seed: int,
) -> list[LoadMessage]:
    """生成可复现的请求序列。

    规则:
        1. 先构造 `ceil(count * (1 - repeat_ratio))` 条首次消息（无放回；不足时循环取样）；
        2. 重复消息从“首次消息中的 repeat_friendly 项”有放回抽取；
        3. 打乱顺序。

    为什么重复消息必须来自首次消息:
        若重复消息没有对应的首次请求，它在序列中只会出现一次，缓存永远无法命中，
        统计出的命中率会系统性偏低。原始设计里“随机有放回抽取”容易产生这种失真，
        这里收紧为“首次集合 ∩ repeat_friendly”，保证每条重复消息至少出现两次。

    参数:
        messages: 消息池。
        count: 请求总数。
        repeat_ratio: 重复消息比例。
        seed: 随机种子。

    返回:
        LoadMessage 列表，长度等于 count。

    异常:
        ValueError: 消息池为空、count <= 0、比例越界，或需要重复消息但没有
            repeat_friendly 消息。
    """
    if not messages:
        raise ValueError("压测消息池为空")
    if count <= 0:
        raise ValueError("请求总数必须为正数")
    if not 0.0 <= repeat_ratio <= 1.0:
        raise ValueError("repeat_ratio 必须在 0~1 之间")

    rng = random.Random(seed)
    first_count = min(count, max(1, math.ceil(count * (1.0 - repeat_ratio))))
    repeats_count = count - first_count

    # 池子可能小于 first_count：循环无放回取样，保证每条消息都有机会出现
    firsts: list[LoadMessage] = []
    while len(firsts) < first_count:
        batch_size = min(len(messages), first_count - len(firsts))
        firsts.extend(rng.sample(messages, batch_size))

    repeat_pool = [message for message in messages if message.repeat_friendly]
    if repeats_count > 0 and not repeat_pool:
        raise ValueError("需要重复消息，但消息池中没有 repeat_friendly=true 的消息")

    repeat_candidates = [message for message in firsts if message.repeat_friendly]
    if repeats_count > 0 and not repeat_candidates:
        # 首次消息恰好都是不可重复项：用一条可重复消息替换，保证重复请求能命中
        replaceable = [
            index for index, message in enumerate(firsts) if not message.repeat_friendly
        ]
        if not replaceable:
            raise ValueError("无法构造可命中缓存的请求序列")
        firsts[replaceable[0]] = rng.choice(repeat_pool)
        repeat_candidates = [firsts[replaceable[0]]]

    sequence = list(firsts)
    for _ in range(repeats_count):
        sequence.append(rng.choice(repeat_candidates))

    rng.shuffle(sequence)
    return sequence

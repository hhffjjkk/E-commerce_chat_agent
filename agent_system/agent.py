# agent.py
# AI 客服 Agent 核心：理解意图 -> 调用工具 -> 生成回复。
# 提供两种后端：
#   1. MockLLM：纯本地规则，无需 API Key，用于跑通闭环/演示。
#   2. LLMClient：真实调用 OpenAI 兼容接口（默认 DeepSeek），支持函数调用与流式输出。
#
# 可观测性接入点（重构后内联，不再依赖外部 monkey-patch）：
#   - 每次 chat.completions.create 前后记录 llm_call；
#   - 异常统一映射为 LLMError（超时 / 429 / 5xx 可重试，其余 4xx 不可重试）；
#   - 工具调用的 tool_call 埋点由 tools.py 负责，本文件不重复记录。

import json
import os
import re
import time

from agent_system.errors.exceptions import LLMError, ToolExecutionError
from agent_system.harness.context_assembler import (
    AssembledContext,
    get_context_assembler,
)
from agent_system.harness.tool_registry import get_registry
from agent_system.obs.events import log_event
from agent_system.tools import list_product_keywords

# 系统提示词：定义角色、边界与平台规则（真实 LLM 模式使用）
SYSTEM_PROMPT = """你是「XX旗舰店」的智能客服，只回答商品、订单、物流、售后相关问题。
规则：
1. 涉及退款、取消订单等敏感操作，先向用户确认订单号无误，得到用户明确确认后再调用工具。
2. 无法处理时，礼貌引导转人工。
3. 不承诺平台之外的优惠或价格。
4. 回答退换货、发货时间、运费、保修、发票等政策类问题时，先调用 search_faq 工具查询店铺规则，再基于查询结果回答。
5. 遇到用户投诉、情绪激动、要求人工或问题无法解决时，主动引导转人工客服。
"""

# 转人工检测：命中敏感词或情绪激动时触发
ESCALATION_KEYWORDS = ["人工", "投诉", "差评", "举报", "曝光", "欺骗", "维权", "没人管"]


def detect_escalation(message: str) -> bool:
    """检测用户消息是否应转人工（命中敏感词或情绪激动）。"""
    if any(k in message for k in ESCALATION_KEYWORDS):
        return True
    if message.count("!") >= 3 or message.count("！") >= 3:
        return True
    return False


def _is_timeout(exc: Exception) -> bool:
    """判断异常是否属于超时类（可重试）。

    参数:
        exc: 原始异常。

    返回:
        True 表示超时类异常。

    说明:
        不直接 import openai/httpx 的异常类，改用“类名包含 timeout”的鸭子判断，
        这样既覆盖 `openai.APITimeoutError`、`httpx.TimeoutException`，也避免
        因 SDK 版本差异导致 ImportError。
    """
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def _status_code_of(exc: Exception) -> int | None:
    """从异常对象中尽力提取 HTTP 状态码。

    参数:
        exc: 原始异常（openai / httpx 的 APIStatusError 等）。

    返回:
        状态码整数；取不到时返回 None。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _map_llm_exception(exc: Exception) -> LLMError:
    """把 LLM 调用异常映射为带 retryable 标记的领域异常。

    参数:
        exc: `chat.completions.create` 抛出的原始异常。

    返回:
        LLMError 实例；原始异常保存在 cause 中，仅用于日志。

    说明:
        - 超时属于暂时性故障 -> 可重试；
        - 429（限流）与 5xx（上游故障）-> 可重试；
        - 其余 4xx（参数/鉴权问题）-> 不可重试，重试没有意义；
        - 无法识别状态码的异常按可重试处理，交给阶段三的 Retry 策略兜底。
    """
    status = _status_code_of(exc)
    if _is_timeout(exc):
        retryable = True
    elif status is not None:
        retryable = status == 429 or status >= 500
    else:
        retryable = True
    return LLMError(retryable=retryable, cause=exc)


class MockLLM:
    """本地规则引擎：模拟「理解意图 -> 调用工具 -> 组织回复」的完整闭环。"""

    @staticmethod
    def _num(n):
        """把 129.0 这类整数浮点显示为 129，保留真正的小数。"""
        if isinstance(n, float) and n.is_integer():
            return int(n)
        return n

    def reply(self, message: str, history: list | None = None) -> str:
        # Mock 后端基于关键词规则，暂不使用历史上下文
        msg = message.strip()
        # 与真实 LLM 路径保持一致：工具统一经 Registry 分发，
        # 这样 disable() 对 Mock 模式同样生效（正常启用时行为不变）。
        registry = get_registry()

        # 1. 优先识别快递单号（物流查询），支持顺丰/圆通/中通/韵达/申通/京东
        tracking_match = re.search(r"(?:SF|YT|ZT|YD|STO|JD)\d{8,}", msg)
        if tracking_match:
            data = registry.call("query_logistics", tracking_no=tracking_match.group())
            return self._format_logistics(data)

        # 2. 识别 11 位订单号（订单查询）
        order_match = re.search(r"\d{11}", msg)
        if order_match:
            data = registry.call("query_order", order_id=order_match.group())
            return self._format_order(data)

        # 3. 商品咨询（按导入数据里的商品名/关键词命中）
        for key in list_product_keywords():
            if key in msg:
                data = registry.call("query_product", name=key)
                return self._format_product(data)

        # 4. 兜底回复
        return (
            "抱歉，我暂时没理解您的问题。您可以这样问我：\n"
            "· 商品：手机多少钱 / 耳机有货吗\n"
            "· 订单：查订单 20261006001\n"
            "· 物流：单号 SF123456789 到哪了"
        )

    def reply_stream(self, message: str, history: list | None = None):
        """Mock 后端不支持真正的流式，直接一次性返回完整回复。"""
        yield self.reply(message, history)

    def _format_product(self, data: dict) -> str:
        if not data["found"]:
            return "抱歉，没找到您要的商品，您可以换个关键词再试。"
        stock = "有货" if data["stock"] > 0 else "暂时缺货"
        return (
            f"{data['name']}：{data['desc']}\n"
            f"价格：¥{self._num(data['price'])}\n"
            f"库存：{stock}（剩余 {self._num(data['stock'])} 件）"
        )

    def _format_order(self, data: dict) -> str:
        if not data["found"]:
            return "抱歉，没有查到该订单，请核对订单号是否正确。"
        logistics = (
            f"{data['logistics_company']}（单号 {data['tracking_no']}）"
            if data["logistics_company"]
            else "尚未发货"
        )
        return (
            f"订单 {data['order_id']} 当前状态：{data['status']}\n"
            f"收货人：{data['receiver']}，金额：¥{self._num(data['amount'])}\n"
            f"物流：{logistics}"
        )

    def _format_logistics(self, data: dict) -> str:
        if not data["found"]:
            return "抱歉，没有查到该快递单号的物流信息。"
        steps = " → ".join(data["steps"])
        return f"快递单号 {data['tracking_no']} 物流轨迹：{steps}"


class LLMClient:
    """OpenAI 兼容 LLM 后端（默认 DeepSeek），支持函数调用与流式输出。"""

    @property
    def TOOLS(self) -> list[dict]:
        """从 Tool Registry 动态获取 OpenAI tools 参数。

        返回:
            当前已启用工具的 schema 列表（含 type/function/name/description/parameters）。

        说明:
            不再把 schema 硬编码在类属性中，避免与 `tools.TOOL_SCHEMAS` 双份维护；
            禁用某个工具后，下一次请求生成的 tools 会立即变化。
            注意：这里是实例属性（property），项目内所有调用均为 `self.TOOLS`，
            不存在 `LLMClient.TOOLS` 的类级访问（已全局检索确认）。
        """
        return get_registry().schemas()

    def __init__(self):
        from openai import OpenAI  # 延迟导入，避免未安装 openai 包时影响 Mock 模式

        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("未检测到 API Key，请先设置 DEEPSEEK_API_KEY 环境变量")

        base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        # 最近一次上下文组装结果；供调试/后续 trace 元数据使用，不进入对外响应
        self._last_assembled: AssembledContext | None = None

    def _call_llm(self, *, stream: bool = False, **kwargs):
        """调用 `chat.completions.create`，统一完成埋点与异常分类。

        参数:
            stream: 是否为流式调用；仅在为 True 时向 SDK 传 `stream=True`，
                保持非流式调用参数与重构前完全一致。
            **kwargs: 透传给 SDK 的参数（model / messages / tools 等）。

        返回:
            SDK 返回的响应对象或流对象。

        异常:
            LLMError: 原始 SDK 异常按超时/状态码分类后的领域异常。

        说明:
            `stream=True` 时 latency_ms 只覆盖“拿到流对象”的耗时，不包含逐块消费，
            与 phase2-step-1 notes 第 10 节的口径保持一致。
        """
        model = kwargs.get("model") or getattr(self, "model", None)
        start = time.perf_counter()
        log_event("llm_call", phase="start", model=model, stream=stream)
        try:
            if stream:
                kwargs["stream"] = True
            result = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 只记录原始异常类型；分类结果通过 LLMError 交给上层错误体系
            log_event(
                "llm_call",
                phase="end",
                model=model,
                stream=stream,
                ok=False,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                error_type=type(exc).__name__,
            )
            raise _map_llm_exception(exc) from exc
        log_event(
            "llm_call",
            phase="end",
            model=model,
            stream=stream,
            ok=True,
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return result

    def _call_tool(self, name: str, arguments: str) -> str:
        """把模型请求的工具名与参数交给 Tool Registry 执行。

        参数:
            name: OpenAI function name。
            arguments: 模型返回的 JSON 字符串（可能为空）。

        返回:
            工具结果的 JSON 字符串。

        异常:
            ToolExecutionError: 参数不是合法 JSON 对象，或 Registry 中工具缺失/禁用。

        说明:
            - arguments 原文不会进入异常消息，避免把模型输出或敏感值带进响应；
            - tool_call 埋点仍由 tools.py 的 `_tool_guard` 统一负责，这里不重复记录。
        """
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            # 模型可能返回非法 JSON：转成领域异常，由统一错误体系输出 tool_failed
            raise ToolExecutionError(tool=name, args_keys=[], cause=exc) from exc
        if not isinstance(args, dict):
            # OpenAI 约定 arguments 解析后应为对象；数组/标量视为模型输出异常
            raise ToolExecutionError(
                "工具参数必须为对象",
                tool=name,
                args_keys=[],
                cause=TypeError("tool arguments must be a JSON object"),
            )
        result = get_registry().call(name, **args)
        return json.dumps(result, ensure_ascii=False)

    def _build_messages(self, message: str, history: list | None) -> list:
        """构造发送给 LLM 的 messages。

        本方法已委托给 ContextAssembler，负责：
        - system prompt 逐字保持（稳定前缀，供阶段五语义缓存使用）；
        - 历史窗口与 token 预算裁剪；
        - 单条 user 截断；
        - 生成 `context_assembled` 结构化日志事件。

        参数:
            message: 当前用户输入。
            history: 存储层返回的历史消息，可为空。

        返回:
            组装后的 messages 列表。
        """
        assembled = get_context_assembler().assemble(
            system_prompt=SYSTEM_PROMPT,
            history=history or [],
            user_message=message,
            tools_schemas=get_registry().schemas(),
        )
        # 缓存最近一次组装结果，便于调试；不改变 AssembledContext 的字段语义
        self._last_assembled = assembled
        return assembled.messages

    def _run_tool_loop(self, messages: list) -> list:
        """执行工具调用循环，返回已包含工具结果的 messages 列表。"""
        for _ in range(5):  # 最多 5 轮，防止死循环
            resp = self._call_llm(
                stream=False,
                model=self.model,
                messages=messages,
                tools=self.TOOLS,
                tool_choice="auto",
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return messages  # 模型不再需要工具，准备生成最终回复
            messages.append(msg)
            for call in msg.tool_calls:
                result = self._call_tool(call.function.name, call.function.arguments)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )
        return messages

    def reply(self, message: str, history: list | None = None) -> str:
        """非流式：返回完整回复。"""
        messages = self._run_tool_loop(self._build_messages(message, history))
        resp = self._call_llm(stream=False, model=self.model, messages=messages)
        return resp.choices[0].message.content or ""

    def reply_stream(self, message: str, history: list | None = None):
        """流式：逐块产出最终回复文本。"""
        messages = self._run_tool_loop(self._build_messages(message, history))
        stream = self._call_llm(stream=True, model=self.model, messages=messages)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

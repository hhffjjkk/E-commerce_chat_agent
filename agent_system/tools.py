"""业务工具层：对外暴露给 Agent 的函数，内部委托给数据源。

接入点说明：
- 每个工具通过 `_tool_guard` 装饰器统一完成「埋点 + 异常分类」；
- 埋点放在本层而不是 Agent 层，是因为 MockLLM 会直接调用工具函数，
  只有在这里记录才能保证 Mock / 真实 LLM 两条路径都产生同样一条 tool_call 事件；
- 业务失败（如 `{"found": False}`）是正常返回值，不会抛异常，也不改写返回值。
"""

import functools
import inspect
import json
import os
import time

from agent_system.config import get_settings
from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError, DataSourceError, ToolExecutionError
from agent_system.harness.retry_executor import RetryExecutor
from agent_system.harness.retry_policy import get_retry_policy
from agent_system.harness.retry_targets import IDEMPOTENT_READ_TOOLS
from agent_system.obs.events import log_event
from agent_system.obs.request_id import get_session_id

# 数据源可能是 httpx（ApiDataSource 未来实现）或 requests（当前骨架），
# 两者都尝试导入；依赖缺失时降级为空元组，不影响本地 JSON 模式。
try:
    import httpx

    _HTTP_ERRORS = (httpx.HTTPError,)
except ImportError:  # pragma: no cover - 极端环境
    _HTTP_ERRORS = ()

try:
    import requests

    _HTTP_ERRORS = _HTTP_ERRORS + (requests.RequestException,)
except ImportError:  # pragma: no cover - 极端环境
    pass

# JSON 损坏、字段缺失、数据文件缺失都归为“工具执行失败”
_DATA_ERRORS = (json.JSONDecodeError, KeyError, FileNotFoundError)


def _tool_guard(name: str):
    """构造工具包装装饰器：统一埋点并映射领域异常。

    参数:
        name: 工具名，用于日志与错误 details。

    返回:
        装饰器函数；被装饰函数保持原签名、原返回值与原名。

    说明:
        捕获顺序很重要：
        1. AgentError 原样抛出，避免重复包装；
        2. 数据文件类错误 -> ToolExecutionError；
        3. HTTP 类错误 -> DataSourceError（可重试）；
        4. 其他异常兜底 -> ToolExecutionError。
        正常返回值（包括业务上的 found=False）一律不拦截。
    """

    def decorator(func):
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):  # pragma: no cover - 内置函数等极端情况
            signature = None

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            ok = True
            error_type = None

            def _invoke():
                """执行一次工具实现，并把原始异常映射为领域异常。"""
                try:
                    return func(*args, **kwargs)
                except AgentError:
                    # 已是领域异常：原样抛出，避免重复包装
                    raise
                except _DATA_ERRORS as exc:
                    # 数据文件缺失/结构异常：归为工具执行失败
                    raise ToolExecutionError(
                        tool=name,
                        args_keys=_args_keys(signature, args, kwargs),
                        cause=exc,
                    ) from exc
                except Exception as exc:
                    # HTTP 类错误来自数据源，单独归为 DataSourceError（可重试）
                    if _HTTP_ERRORS and isinstance(exc, _HTTP_ERRORS):
                        raise DataSourceError(tool=name, cause=exc) from exc
                    raise ToolExecutionError(
                        tool=name,
                        args_keys=_args_keys(signature, args, kwargs),
                        cause=exc,
                    ) from exc

            try:
                if name in IDEMPOTENT_READ_TOOLS:
                    # 仅幂等读工具走重试；重试判定由 RetryPolicy 与错误码决定。
                    # 写操作/确认工具不在白名单，重试可能造成重复副作用。
                    executor = RetryExecutor(get_retry_policy(), target=f"tool:{name}")
                    return executor.run(_invoke)
                return _invoke()
            except AgentError as exc:
                # 业务调用最终失败：tool_call 只记录一次，重试尝试由 retry_attempt 记录
                ok = False
                error_type = exc.code
                raise
            finally:
                # finally 保证成功/失败都恰好产生一条 tool_call 事件；
                # error_type 仅在失败时输出，保持与重构前日志字段一致
                fields = {
                    "tool": name,
                    "args_keys": _args_keys(signature, args, kwargs),
                    "ok": ok,
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                }
                if error_type:
                    fields["error_type"] = error_type
                log_event("tool_call", **fields)

        return wrapper

    return decorator


def _args_keys(signature, args, kwargs) -> list[str]:
    """收集本次实际传入的参数名（只记键名，不记值）。"""
    if signature is None:
        return []
    try:
        return sorted(signature.bind_partial(*args, **kwargs).arguments)
    except TypeError:
        return []


def map_tool_exception(tool: str, exc: Exception, args_keys) -> AgentError:
    """把工具实现抛出的异常映射为领域异常。

    参数:
        tool: 工具名，写入 details。
        exc: 原始异常。
        args_keys: 参数名列表（只记键名）。

    返回:
        AgentError 实例（AgentError 原样返回，其余按类型映射）。

    说明:
        Guardrails.confirm 在绕过对外工具直接调用 impl 时复用它，
        保证“确认执行”与“直接执行”的异常语义一致。
    """
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, _DATA_ERRORS):
        return ToolExecutionError(tool=tool, args_keys=list(args_keys), cause=exc)
    if _HTTP_ERRORS and isinstance(exc, _HTTP_ERRORS):
        return DataSourceError(tool=tool, cause=exc)
    return ToolExecutionError(tool=tool, args_keys=list(args_keys), cause=exc)


def _get_source():
    """根据环境变量选择数据源：有 API 配置则用真实接口，否则用本地 JSON。"""
    from agent_system.datasource import ApiDataSource, JsonDataSource

    base_url = os.getenv("API_BASE_URL")
    token = os.getenv("API_TOKEN")
    if base_url and token:
        return ApiDataSource(base_url, token)
    return JsonDataSource()


_source = _get_source()


@_tool_guard("query_product")
def query_product(name: str) -> dict:
    return _source.query_product(name)


@_tool_guard("query_order")
def query_order(order_id: str) -> dict:
    return _source.query_order(order_id)


@_tool_guard("query_logistics")
def query_logistics(tracking_no: str) -> dict:
    return _source.query_logistics(tracking_no)


@_tool_guard("search_faq")
def search_faq(question: str) -> dict:
    return _source.search_faq(question)


def _apply_refund_impl(order_id: str, reason: str) -> dict:
    """退款写操作的真实业务实现。

    说明:
        不加上 `_tool_guard`：对外工具 apply_refund 负责埋点/异常映射；
        Guardrails.confirm 直接调用本函数执行，避免二次拦截。
    """
    return _source.apply_refund(order_id, reason)


def _cancel_order_impl(order_id: str) -> dict:
    """取消订单写操作的真实业务实现（同上，不经过对外拦截）。"""
    return _source.cancel_order(order_id)


@_tool_guard("apply_refund")
def apply_refund(order_id: str, reason: str) -> dict:
    """为订单申请退款。

    首次调用只创建服务端待确认记录并返回 `needs_confirmation=True`，不访问数据源；
    用户明确确认后由 confirm_action → Guardrails.confirm 调用 `_apply_refund_impl` 执行。

    参数:
        order_id: 订单号。
        reason: 退款原因。

    返回:
        待确认结构（needs_confirmation/action_id/summary/expires_in）；
        CONFIRMATION_ENABLED=false 时返回真实执行结果。

    异常:
        AgentError: 无会话上下文（confirmation_required）或执行失败。
    """
    # 应急开关：关闭两阶段时直接执行，行为与重构前一致
    if not get_settings().confirmation_enabled:
        return _apply_refund_impl(order_id, reason)

    session_id = get_session_id()
    if not session_id:
        # 没有会话上下文时不静默执行写操作，避免绕过确认
        raise AgentError(
            ErrorCode.CONFIRMATION_REQUIRED,
            details={"tool": "apply_refund"},
        )

    # 延迟导入：tools 与 guardrails 互相引用，放在函数内避免模块循环
    from agent_system.harness.guardrails import get_guardrails

    return get_guardrails().intercept_write(
        "apply_refund",
        {"order_id": order_id, "reason": reason},
        session_id,
    )


@_tool_guard("cancel_order")
def cancel_order(order_id: str) -> dict:
    """取消订单（两阶段确认）。

    首次调用仅创建待确认记录；确认后由 confirm_action 触发 `_cancel_order_impl`。

    参数:
        order_id: 订单号。

    返回:
        待确认结构；CONFIRMATION_ENABLED=false 时返回真实执行结果。

    异常:
        AgentError: 无会话上下文（confirmation_required）或执行失败。
    """
    if not get_settings().confirmation_enabled:
        return _cancel_order_impl(order_id)

    session_id = get_session_id()
    if not session_id:
        raise AgentError(
            ErrorCode.CONFIRMATION_REQUIRED,
            details={"tool": "cancel_order"},
        )

    from agent_system.harness.guardrails import get_guardrails

    return get_guardrails().intercept_write(
        "cancel_order",
        {"order_id": order_id},
        session_id,
    )


@_tool_guard("list_product_keywords")
def list_product_keywords() -> list:
    return _source.list_product_keywords()


# ---------------------------------------------------------------------------
# 工具 schema 常量：从重构前 agent.py 的 LLMClient.TOOLS 原样迁移。
# 每个 schema 只保留 function 里的 description / parameters，name 由字典 key 提供。
# 禁止在此处增删字段或改写文案：schema 是模型行为契约，变更需单独立项评估。
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: dict[str, dict] = {
    # 迁移自 LLMClient.TOOLS[0]（query_product），字段与文案逐字保留
    "query_product": {
        "description": "根据商品名查询商品的价格、库存和描述",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "商品名称，如 手机、耳机"}
            },
            "required": ["name"],
        },
    },
    # 迁移自 LLMClient.TOOLS[1]（query_order），字段与文案逐字保留
    "query_order": {
        "description": "根据订单号查询订单状态、金额和物流信息",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "11 位订单号"}
            },
            "required": ["order_id"],
        },
    },
    # 迁移自 LLMClient.TOOLS[2]（query_logistics），字段与文案逐字保留
    "query_logistics": {
        "description": "根据快递单号查询完整物流轨迹",
        "parameters": {
            "type": "object",
            "properties": {
                "tracking_no": {"type": "string", "description": "快递单号，如 SF123456789"}
            },
            "required": ["tracking_no"],
        },
    },
    # 迁移自 LLMClient.TOOLS[3]（search_faq），字段与文案逐字保留
    "search_faq": {
        "description": "检索店铺 FAQ 知识库，回答退换货、发货、运费、保修、发票等政策类问题",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户提出的政策类问题"}
            },
            "required": ["question"],
        },
    },
    # 迁移自 LLMClient.TOOLS[4]（apply_refund），字段与文案逐字保留
    "apply_refund": {
        "description": "为订单申请退款/退货，把订单状态更新为退款中（敏感操作，需先核对订单信息）",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "reason": {"type": "string", "description": "退款/退货原因"},
            },
            "required": ["order_id", "reason"],
        },
    },
    # 迁移自 LLMClient.TOOLS[5]（cancel_order），字段与文案逐字保留
    "cancel_order": {
        "description": "取消订单，仅待支付订单可取消",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"}
            },
            "required": ["order_id"],
        },
    },
    # 阶段三步骤 4 新增：确认/拒绝被拦截的写操作（内部工具）
    "confirm_action": {
        "description": "确认之前被服务端拦截的写操作并执行。仅在用户明确确认后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "intercept 阶段返回的 action_id",
                }
            },
            "required": ["action_id"],
        },
    },
    "reject_action": {
        "description": "拒绝之前被服务端拦截的写操作并作废。用户反悔或拒绝时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "intercept 阶段返回的 action_id",
                }
            },
            "required": ["action_id"],
        },
    },
}

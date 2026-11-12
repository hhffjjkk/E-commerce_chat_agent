# agent_system/errors/codes.py
# 错误码体系：码 -> HTTP 状态 / 默认 retryable / 中文短消息，集中维护。
# 约定：任何地方都不要再手写 HTTP 状态与中文文案，统一从这里查表，避免多处不一致。

from enum import Enum


class ErrorCode(str, Enum):
    """业务错误码枚举：继承 str，便于直接与字符串比较和序列化。"""

    INTERNAL_ERROR = "internal_error"
    TOOL_FAILED = "tool_failed"
    LLM_FAILED = "llm_failed"
    DATASOURCE_FAILED = "datasource_failed"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"


# 错误码 -> HTTP 状态码
_HTTP_STATUS = {
    ErrorCode.INTERNAL_ERROR.value: 500,
    ErrorCode.TOOL_FAILED.value: 500,
    ErrorCode.LLM_FAILED.value: 502,
    ErrorCode.DATASOURCE_FAILED.value: 502,
    ErrorCode.VALIDATION_ERROR.value: 400,
    ErrorCode.RATE_LIMITED.value: 429,
}

# 错误码 -> 是否默认值得自动重试（阶段三的 Retry 决策依据）
# 判断标准：失效是「暂时性」的才可重试；参数错误、代码缺陷重试无意义。
_RETRYABLE = {
    ErrorCode.INTERNAL_ERROR.value: False,
    ErrorCode.TOOL_FAILED.value: True,
    ErrorCode.LLM_FAILED.value: True,
    ErrorCode.DATASOURCE_FAILED.value: True,
    ErrorCode.VALIDATION_ERROR.value: False,
    ErrorCode.RATE_LIMITED.value: True,
}

# 错误码 -> 面向调用方的中文短消息（不含原始异常文本）
_MESSAGES = {
    ErrorCode.INTERNAL_ERROR.value: "服务内部错误",
    ErrorCode.TOOL_FAILED.value: "工具执行失败",
    ErrorCode.LLM_FAILED.value: "模型服务调用失败",
    ErrorCode.DATASOURCE_FAILED.value: "数据源访问失败",
    ErrorCode.VALIDATION_ERROR.value: "请求参数非法",
    ErrorCode.RATE_LIMITED.value: "请求过于频繁，请稍后再试",
}

# 纯 HTTP 状态（4xx/5xx）-> 中文短消息，用于 http_{status} 兜底码
_HTTP_MESSAGES = {
    400: "请求参数非法",
    401: "未授权",
    403: "无访问权限",
    404: "资源不存在",
    405: "请求方法不允许",
    409: "请求冲突",
    429: "请求过于频繁，请稍后再试",
}


def normalize_code(code) -> str:
    """把 ErrorCode / 字符串统一成字符串码，便于字典查表与 JSON 序列化。"""
    if isinstance(code, ErrorCode):
        return code.value
    return str(code)


def _http_code_status(code: str) -> int | None:
    """从形如 "http_404" 的兜底码里解析出状态码；不是该形态则返回 None。"""
    if not code.startswith("http_"):
        return None
    try:
        return int(code[len("http_") :])
    except ValueError:
        return None


def http_status_for(code) -> int:
    """查询错误码对应的 HTTP 状态；未知码与非法 http_{status} 一律按 500 处理。"""
    value = normalize_code(code)
    if value in _HTTP_STATUS:
        return _HTTP_STATUS[value]
    return _http_code_status(value) or 500


def retryable_for(code) -> bool:
    """查询错误码是否可重试。

    业务码查表；http_{status} 兜底码按「5xx 或 429 可重试」推断；
    完全未知的码默认 True（宁可重试一次，也不轻易放弃）。
    """
    value = normalize_code(code)
    if value in _RETRYABLE:
        return _RETRYABLE[value]
    status = _http_code_status(value)
    if status is not None:
        return status >= 500 or status == 429
    return True


def message_for(code) -> str:
    """查询错误码的中文短消息；http_{status} 按状态映射，未知码给通用文案。"""
    value = normalize_code(code)
    if value in _MESSAGES:
        return _MESSAGES[value]
    status = _http_code_status(value)
    if status is not None:
        return _HTTP_MESSAGES.get(status, "请求失败")
    return "请求失败"


def http_message_for(status: int) -> str:
    """根据状态码给出中文短消息：5xx 统一说内部错误，不暴露实现细节。"""
    if status >= 500:
        return "服务内部错误"
    return _HTTP_MESSAGES.get(status, "请求失败")

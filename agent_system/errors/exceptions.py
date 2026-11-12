# agent_system/errors/exceptions.py
# 领域异常：统一携带错误码、HTTP 状态、可安全外露的 details 与原始 cause。
# 核心约束：
#   details 只放可外露字段（工具名、参数名、字段名），不放参数值/堆栈/LLM 原文；
#   cause 仅用于日志（只记录异常类型），永远不进入响应体。

from agent_system.errors.codes import (
    ErrorCode,
    http_status_for,
    message_for,
    normalize_code,
    retryable_for,
)


class AgentError(Exception):
    """所有领域异常的基类：一个错误码 + 可选细节 + 可选的原始异常。"""

    def __init__(
        self,
        code,
        message: str | None = None,
        *,
        details: dict | None = None,
        retryable: bool | None = None,
        cause: Exception | None = None,
    ):
        """构造领域异常。

        参数：
            code: 错误码（ErrorCode 或字符串，如 "http_404"）。
            message: 面向调用方的中文短消息；不传则按错误码查默认文案。
            details: 可安全外露的附加信息，默认空字典（响应里保证是 {} 而不是 null）。
            retryable: 是否可重试；不传则按错误码查默认值。
            cause: 原始异常，仅供日志记录类型，不输出到响应体。
        """
        self.code = normalize_code(code)
        self.message = message or message_for(self.code)
        self.details = dict(details or {})
        self.retryable = retryable_for(self.code) if retryable is None else bool(retryable)
        self.cause = cause
        # HTTP 状态由错误码查表得出，避免各处散写
        self.http_status = http_status_for(self.code)
        super().__init__(self.message)

    def to_payload(self, request_id: str) -> dict:
        """生成统一错误响应体；details 为空时输出 {}，绝不输出 null。"""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": request_id or "-",
                "retryable": self.retryable,
                "details": self.details or {},
            }
        }

    def __str__(self) -> str:
        """异常字符串只返回中文消息，避免把细节带到日志的 error_message 里。"""
        return self.message


class ToolExecutionError(AgentError):
    """工具执行失败：把工具名与参数键固化进 details，便于定位是哪个工具坏了。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        tool: str | None = None,
        args_keys=None,
        code=ErrorCode.TOOL_FAILED,
        retryable: bool | None = None,
        cause: Exception | None = None,
        details: dict | None = None,
    ):
        """在基类基础上补充 tool / args_keys 两个可外露字段（只记键名不记值）。"""
        merged = dict(details or {})
        if tool is not None:
            merged["tool"] = tool
        if args_keys is not None:
            merged["args_keys"] = list(args_keys)
        super().__init__(code, message, details=merged, retryable=retryable, cause=cause)


class LLMError(AgentError):
    """LLM 调用失败：统一 llm_failed / 502，retryable 由包装层按异常形态决定。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        code=ErrorCode.LLM_FAILED,
        retryable: bool | None = None,
        cause: Exception | None = None,
        details: dict | None = None,
    ):
        """默认不可外露任何模型原始响应，details 一般留空。"""
        super().__init__(code, message, details=details, retryable=retryable, cause=cause)


class DataSourceError(AgentError):
    """数据源访问失败：外部接口连不上或超时，与「我们自己代码写错」区分开。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        tool: str | None = None,
        code=ErrorCode.DATASOURCE_FAILED,
        retryable: bool | None = None,
        cause: Exception | None = None,
        details: dict | None = None,
    ):
        """补充 tool 字段，说明是哪个工具触发的数据源访问。"""
        merged = dict(details or {})
        if tool is not None:
            merged["tool"] = tool
        super().__init__(code, message, details=merged, retryable=retryable, cause=cause)


class ValidationError(AgentError):
    """请求参数非法：固定 400 + 不可重试，details 通常放缺失的字段名列表。"""

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict | None = None,
        retryable: bool | None = None,
        cause: Exception | None = None,
    ):
        """错误码固定为 validation_error，重试与否由调用方按需覆盖。"""
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            message,
            details=details,
            retryable=retryable,
            cause=cause,
        )

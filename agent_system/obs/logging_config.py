# agent_system/obs/logging_config.py
# 标准库 logging 的 JSON / 文本格式化配置，不引入第三方日志框架。
# 目标：stdout 每行一条 JSON，字段稳定，便于后续采集与检索。

import json
import logging
import sys
from datetime import datetime, timezone

# 应用统一使用的 logger 名称，避免污染 root logger 与第三方库日志
LOGGER_NAME = "ecommerce_agent"

# LogRecord 自带属性集合：格式化时需要排除，否则会把内部字段也塞进 JSON
_STANDARD_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


def _utc_iso() -> str:
    """生成 UTC 的 ISO8601 时间戳（毫秒精度，Z 结尾），跨机器可排序。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _extra_fields(record: logging.LogRecord) -> dict:
    """取出调用方通过 extra={...} 传入的自定义字段。

    排除 LogRecord 自带属性、下划线私有字段，以及已在 JSON 头部固定输出的
    service / event / request_id（避免同一字段在一行里出现两次）。
    """
    fields = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        if key in {"service", "event", "request_id"}:
            continue
        fields[key] = value
    return fields


class JSONFormatter(logging.Formatter):
    """每行输出一个 JSON 对象，字段稳定、便于采集。"""

    def __init__(self, service: str):
        """保存服务名，作为每条日志的 service 字段。"""
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """把一条日志记录渲染成单行 JSON。

        固定字段：ts / level / service / request_id / event；
        request_id 缺失时输出 "-"（不抛异常，日志永远不能反过来打断业务）。
        """
        payload = {
            "ts": _utc_iso(),
            "level": record.levelname,
            "service": self.service,
            "request_id": getattr(record, "request_id", "-") or "-",
            "event": getattr(record, "event", record.getMessage()),
        }
        payload.update(_extra_fields(record))
        # default=str 兜底：无法序列化的对象退化成字符串，而不是让整条日志丢掉
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """本地调试用的可读格式（LOG_FORMAT=text 时启用）。"""

    def __init__(self, service: str):
        """保存服务名，用于在每行前缀里标明来源。"""
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """渲染成「时间 级别 [服务] 事件 request_id=... 其余字段」的单行文本。"""
        request_id = getattr(record, "request_id", "-") or "-"
        event = getattr(record, "event", record.getMessage())
        extras = " ".join(f"{key}={value}" for key, value in _extra_fields(record).items())
        line = f"{_utc_iso()} {record.levelname} [{self.service}] {event} request_id={request_id}"
        return f"{line} {extras}".rstrip()


def configure_logging(settings) -> logging.Logger:
    """初始化应用 logger；重复调用时保持单 handler，避免日志重复输出。

    参数 settings 来自 agent_system.config.get_settings()，用到 service_name / log_level / log_format。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.log_level)
    # 关闭向上传播，否则 root logger 可能再输出一遍（导致同一事件出现两行）
    logger.propagate = False

    # 先清空旧 handler，保证多次调用（如测试热重载）不会叠加输出
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    stream = sys.stdout if sys.stdout is not None else sys.stderr
    handler = logging.StreamHandler(stream)
    if settings.log_format == "text":
        handler.setFormatter(TextFormatter(settings.service_name))
    else:
        handler.setFormatter(JSONFormatter(settings.service_name))
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    """取得应用 logger；不做初始化，调用方需先执行 configure_logging。"""
    return logging.getLogger(LOGGER_NAME)

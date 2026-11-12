# 阶段二 · 步骤 1 实现说明：请求 ID 与结构化日志骨架

> 对应阶段二第 1 步。本步骤只做可观测性骨架，不改业务逻辑、不改 NDJSON 既有字段。
> 启动方式：`uvicorn src.main:app`（本地脚本见 `scripts/dev_run.sh`）。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| vendor 是否被改动 | **否**（`git status` 在 `vendor/ecommerce-customer-service-agent` 下为空） |
| 新增依赖 | 无（运行时仅用标准库；测试用 pytest / httpx，已装在 vendor venv） |
| 请求 ID 贯穿 | HTTP / `/chat` / `/chat/stream` / `/webhook/{platform}` 均生效 |
| 结构化日志 | stdout 每行一条 JSON，`LOG_FORMAT=text` 可回落到可读格式 |
| 流式响应 | 原有两个事件完全不变，仅在末尾追加 `{"event":"done","request_id":"..."}` |
| 测试 | `pytest tests/test_request_id.py` → 6 passed |

## 2. 包装策略

- `src/config.py` 的 `ensure_vendor_path()` 把 `vendor/ecommerce-customer-service-agent` 插入 `sys.path`；
- `src/main.py` 先 `get_settings()` + `configure_logging()`，再导入 vendor 的 `app`、`agent`、`tools`（导入顺序不可调整）；
- `app = vendor_app_module.app` 复用 vendor 的 FastAPI 实例，随后只做三件事：
  1. `app.add_middleware(RequestContextMiddleware)`；
  2. 注册 `StarletteHTTPException` 与 `Exception` 两个异常处理器；
  3. 启动时执行 `instrument_tools()` / `instrument_llm(bot)`（见第 6 节）。
- 因此 vendor 的 `app.py`、`agent.py`、`tools.py` **一行未改**，可随时重新 clone/升级 vendor。

## 3. 请求 ID 中间件

文件：`src/obs/request_id.py`，实现为**纯 ASGI 中间件**（非 `BaseHTTPMiddleware`）。

- 复用入站 `X-Request-ID`，合法格式为 `^[A-Za-z0-9._:-]{8,64}$`；否则生成 `uuid4().hex[:16]`；
- 通过 `contextvars.ContextVar` 暴露 `get_request_id()`（缺省返回 `"-"`）与 `get_session_id()`；
- 响应头统一写入 `X-Request-ID`（在 `http.response.start` 处注入，流式同样生效）；
- 从请求体 JSON 中读取 `session_id`（最多抓取 64KB），后续 `tool_call` / `request_end` 日志自动带上；
- 同时把 `request_id`、`start_perf`、`session_id` 写入 `scope["state"]`，供异常处理器读取。

选择纯 ASGI 的原因：`BaseHTTPMiddleware` 会额外包一层任务组，对 `StreamingResponse` 的响应头/末块拦截、以及 contextvars 在流式场景的传播都更容易出偏差；纯 ASGI 可直接改写 `http.response.start` 与 `http.response.body` 消息。

### 3.1 流式响应的处理

- 仅当 `path == "/chat/stream"`、状态 200、且收到**最后一块** `http.response.body` 时，追加一行 `{"event": "done", "request_id": "<rid>"}`；
- 既有 `{"delta": ...}`、`{"done": true}` 原样保留；不新增路由/工具/耗时字段；
- 若最后一块本身不以换行结尾，会先补 `\n` 再追加，保证 NDJSON 行完整。

## 4. 结构化日志

文件：`src/obs/logging_config.py` + `src/obs/events.py`；标准库 `logging` + 自定义 `JSONFormatter`。

### 4.1 字段表

| 字段 | 出现事件 | 说明 |
| --- | --- | --- |
| `ts` | 全部 | UTC ISO8601，毫秒精度，`Z` 结尾 |
| `level` | 全部 | 日志级别，默认 `INFO` |
| `service` | 全部 | `SERVICE_NAME`，默认 `ecommerce-agent` |
| `request_id` | 全部 | 当前请求 ID；无上下文时为 `-` |
| `event` | 全部 | 事件名，见 4.2 |
| `session_id` | 可得的业务事件 | 来自请求体 `session_id`（`/chat`、`/chat/stream`） |
| `method` / `path` | request_* | HTTP 方法与路径 |
| `status` | request_end | 响应状态码 |
| `latency_ms` | request_end / tool_call / llm_call | 耗时，保留 2 位小数 |
| `tool` | tool_call | 工具名 |
| `args_keys` | tool_call | 参数名列表（**不含参数值**，避免泄露） |
| `ok` | tool_call / llm_call(end) | 是否成功 |
| `phase` | llm_call | `start` / `end` |
| `model` / `stream` | llm_call | 模型名、是否流式 |
| `error_type` / `error_message` | request_error / 失败事件 | 异常类型与截断到 200 字符的消息 |

`log_event(event, **fields)` 自动补齐 `request_id` / `session_id`；任何日志序列化异常都会被吞掉，不影响请求。

### 4.2 本步骤埋点事件

| 事件 | 触发点 | 关键字段 |
| --- | --- | --- |
| `request_start` | 中间件入口 | method / path |
| `request_end` | 响应返回（含异常路径） | status / latency_ms / session_id |
| `request_error` | 未捕获异常，中间件捕获后重抛 | error_type / error_message |
| `llm_call` | `LLMClient` 每次 `chat.completions.create` 前后 | phase / model / stream / ok / latency_ms |
| `tool_call` | 每个工具函数入口（`finally`） | tool / args_keys / ok / latency_ms |

## 5. 异常处理器骨架

文件：`src/main.py`。

- `Exception` → `500`，body 固定为 `{"error": {"code": "internal_error", "request_id": "<rid>"}}`，并记录 `request_end`（带 `error_type`）；
- `StarletteHTTPException` → 保持原状态码，body 为 `{"error": {"code": "http_error", "request_id": "<rid>"}}`；
- 两者都会在响应头回写 `X-Request-ID`；
- FastAPI 的 `RequestValidationError`（422）仍走框架默认处理器，`{"detail": [...]}` 结构不变；
- 正常业务响应（`/chat`、`/chat/stream`、`/webhook`、静态页）body 结构未改动。

异常路径的日志顺序（实测）：`request_start` → `request_error` → `request_end(status=500)`，三者各一次，不重复。

## 6. 埋点实现方式（vendor 零改动）

### 6.1 `tools`：monkey-patch

`src/main.py` 的 `instrument_tools()` 在**启动时（导入阶段）**包装 `tools` 模块中的 7 个函数：

- 用 `functools.wraps` 生成 wrapper，在 `finally` 中记录 `tool_call`；
- 用 `inspect.signature.bind_partial` 取参数名作为 `args_keys`（只记录键名，不记录值）；
- 同步替换 `agent` 模块里 `from tools import ...` 得到的同名全局引用（`agent.py` 是直接导入函数名，且每次调用时才构造 `_call_tool` 映射，因此两处都要替换）；
- 幂等：wrapper 带 `_obs_wrapped` 标记，重复调用不会二次包装。

### 6.2 `LLMClient`：运行期包装 `create`

`instrument_llm(bot)` 不修改 `agent.py` 源码，而是替换实例属性 `bot.client.chat.completions.create`：

- 调用前记 `llm_call(phase="start")`，返回/抛错后记 `llm_call(phase="end")` 并带 `latency_ms`、`ok`；
- 幂等标记 `_obs_instrumented`；
- `MockLLM` 没有 `client`，函数返回 `False`，因此 Mock 模式下**不会产生 `llm_call` 事件**（属预期）。

## 7. 流式响应改动前后对比

对比条件：同一 vendor 版本、同一 Mock 模式、同一 `session_id`/`message`，分别请求 vendor 原始 `app` 与 `src.main:app`。

**改动前（`uvicorn app:app`，端口 8012）**

```text
STATUS 200
X-Request-ID None
LINES 2
{"delta": "抱歉，我暂时没理解您的问题。……"}
{"done": true}
```

**改动后（`uvicorn src.main:app`，端口 8011）**

```text
STATUS 200
X-Request-ID stream-demo-0001
{"delta": "抱歉，我暂时没理解您的问题。……"}
{"done": true}
{"event": "done", "request_id": "stream-demo-0001"}
```

结论：原有 2 行逐字节一致，仅新增第 3 行与响应头。

## 8. 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVICE_NAME` | `ecommerce-agent` | 日志中的 `service` |
| `LOG_LEVEL` | `INFO` | 非法值回落 `INFO` |
| `LOG_FORMAT` | `json` | `json` / `text`，非法值回落 `json` |

`text` 实测输出：

```text
2026-11-09T15:55:07.460Z INFO [ecommerce-agent] demo_event request_id=- foo=1
```

## 9. 验证记录

### 9.1 单元测试

```text
pytest tests/test_request_id.py -q
6 passed in 0.75s
```

覆盖：合法 ID 复用、非法 ID 重新生成（16 位 hex）、响应头存在、日志可解析且含 `request_id`/`session_id`、`tool_call` 参数键、流式末行 `event=done`、统一 500 结构与 `request_error`、`llm_call` 起止事件。

### 9.2 运行时验证

| 场景 | 结果 |
| --- | --- |
| `POST /chat`，入站 `X-Request-ID: trace-demo-0001` | 200；响应头回显同一 ID；stdout 出现 `request_start` / `tool_call` / `request_end` |
| `POST /chat`，入站 `X-Request-ID: bad id!!` | 200；响应头为新生 16 位 ID（`6d6f9ae4d762492c`） |
| `POST /chat/stream` | 200；响应头含 ID；末行 `{"event": "done", "request_id": ...}` |
| `POST /webhook/douyin` | 200；响应头含 ID；日志含 `request_start` / `request_end`，`path=/webhook/douyin` |
| 临时路由抛 `RuntimeError` | 500；`{"error":{"code":"internal_error","request_id":"boom-demo-000001"}}`；日志三事件各一次 |

运行时日志样例：

```json
{"ts": "2026-11-09T15:53:13.838Z", "level": "INFO", "service": "ecommerce-agent", "request_id": "trace-demo-0001", "event": "request_start", "method": "POST", "path": "/chat"}
{"ts": "2026-11-09T15:53:13.857Z", "level": "INFO", "service": "ecommerce-agent", "request_id": "trace-demo-0001", "event": "tool_call", "session_id": "s1", "tool": "list_product_keywords", "args_keys": [], "ok": true, "latency_ms": 0.33}
{"ts": "2026-11-09T15:53:13.864Z", "level": "INFO", "service": "ecommerce-agent", "request_id": "trace-demo-0001", "event": "request_end", "method": "POST", "path": "/chat", "status": 200, "latency_ms": 26.2, "session_id": "s1"}
```

## 10. 遗留问题

1. **`stream=True` 的 `llm_call` 耗时口径不完整**：只覆盖 `create()` 返回流对象的时间，不包含后续逐块消费；属于步骤 4（Tracing/耗时）再细化。
2. **MockLLM 模式无 `llm_call`**：无 `client`，只有 `tool_call` 与请求级事件；真实 LLM 模式下才产生。
3. **`/webhook/{platform}` 的 `session_id` 未进入日志上下文**：`session_id` 由 `channels.normalize()` 在路由内算出（如 `conversation_id`），中间件此时无法得知；如需贯穿，应在后续步骤对 webhook 路由做事件埋点。
4. **响应已开始发送后再抛异常无法改写 body**：此时异常处理器不可能再写统一 JSON，中间件只记 `request_error`，`request_end` 的状态仍是已发出的状态码（流式场景的固有限制）。
5. **异常时 uvicorn 仍会把 traceback 打到 stderr**：这是 `ServerErrorMiddleware` 在返回统一 JSON 后重抛的框架行为；如需彻底静默可后续用 `--log-config` 调整，不影响接口响应。
6. **uvicorn access log 仍是文本**：与应用 JSON 日志分属不同 logger；本地启动可加 `--no-access-log`（`scripts/dev_run.sh` 支持透传 `"$@"`）。
7. **`StarletteHTTPException` 响应体已改为 `{"error": ...}`**：404/405 等错误响应结构变化，正常业务响应不受影响；若后续需要保持原 `{"detail": ...}`，可移除该处理器。

## 11. 复现命令

```bash
# 安装测试依赖（仅测试需要）
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pip install pytest httpx

# 运行测试
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pytest tests/test_request_id.py -q

# 启动服务
bash scripts/dev_run.sh
# 或
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8010

# 验证响应头与日志
curl -s -D - -X POST http://127.0.0.1:8010/chat \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: trace-demo-0001" \
  -d '{"session_id":"s1","message":"你好"}'

# 验证流式末行
curl -s -X POST http://127.0.0.1:8010/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s2","message":"查订单 20261006001"}'
```

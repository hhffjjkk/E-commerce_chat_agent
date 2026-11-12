# 阶段二 · 步骤 2 实现说明：统一错误响应与异常兜底接入点

> 在步骤 1 的 request_id / JSON 日志之上，新增错误码体系、领域异常、全局异常处理器与 FallbackHook 接入点。
> **本步骤不含任何重试、退避、降级逻辑**；这些由阶段三实现，接口在本步骤固定。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| vendor 是否被改动 | **否**（`vendor/ecommerce-customer-service-agent` 下 `git status` 为空） |
| 新增依赖 | 无（`httpx` 仅做可选识别，缺失时退化为类名判断） |
| 新增文件 | `src/errors/{codes,exceptions,fallback,wrappers,handlers}.py`、`tests/test_errors.py` |
| 修改文件 | `src/main.py`、`src/obs/events.py`（新增 `error_raised` 事件构造） |
| 测试 | `pytest tests/test_errors.py tests/test_request_id.py` → **14 passed**（8 + 6） |
| 兜底钩子 | `FallbackHook` 协议 + `NoOpFallbackHook` 默认实现 + `register_hook` / `get_hook` |

## 2. 错误码表（`src/errors/codes.py`）

| 错误码 | HTTP | 默认 retryable | 默认 message |
| --- | --- | --- | --- |
| `internal_error` | 500 | `false` | 服务内部错误 |
| `tool_failed` | 500 | `true` | 工具执行失败 |
| `llm_failed` | 502 | `true` | 模型服务调用失败 |
| `datasource_failed` | 502 | `true` | 数据源访问失败 |
| `validation_error` | 400 | `false` | 请求参数非法 |
| `rate_limited` | 429 | `true` | 请求过于频繁，请稍后再试 |
| `http_{status}` | 原始状态 | 5xx / 429 为 `true`，其余 `false` | 按状态映射（如 404 资源不存在） |

说明：

- HTTP 状态、retryable 默认值、中文短消息全部集中在 `codes.py`，调用方不再散写；
- `http_{status}` 是 `HTTPException` 的兜底码，用于保持原有状态码语义（见 4.3）；
- `rate_limited` 本步骤只保留码与映射，限流逻辑留给步骤 3。

## 3. 领域异常（`src/errors/exceptions.py`）

```text
AgentError(Exception)            # code / message / http_status / details / retryable / cause
  ├── ToolExecutionError         # details: {tool, args_keys}
  ├── LLMError
  ├── DataSourceError            # details: {tool}
  └── ValidationError            # details: {fields}
```

- `AgentError(code, message=None, *, details=None, retryable=None, cause=None)`；`http_status` 由 `code` 查表；
- `retryable` 未显式传入时取 `codes.py` 默认值；
- `details` 只放可安全外露字段（工具名、参数键、校验字段名），**不含参数值、异常文本、堆栈、LLM 响应**；
- `cause` 仅用于日志（只记 `type(cause).__name__`），不进入响应体；
- `to_payload(request_id)` 生成统一结构：

```json
{
  "error": {
    "code": "tool_failed",
    "message": "工具执行失败",
    "request_id": "abc123",
    "retryable": true,
    "details": {"tool": "query_order", "args_keys": ["order_id"]}
  }
}
```

`details` 为空时输出 `{}`，不会出现 `null`。

## 4. 异常捕获范围

### 4.1 全局处理器（`src/errors/handlers.py`）

| 注册的异常 | 输出 | 日志 |
| --- | --- | --- |
| `AgentError`（含 4 个子类） | 状态取 `error.http_status`，body 为 `to_payload` | `error_raised`（code / retryable / cause_type / path / tool / session_id） |
| `RequestValidationError` | **400**，`validation_error`，`details.fields` 为缺失/非法字段名 | `error_raised` |
| `StarletteHTTPException` | 原始状态码；code 优先取 `detail["code"]`，否则 `http_{status}` | `error_raised` |
| `Exception` | 500，`internal_error`（仅 code + request_id，见下） | `error_raised` + `request_end(status=500)` |

所有错误响应都会回写 `X-Request-ID`，且与 body 中 `request_id` 一致。

**与步骤 1 的兼容点**：未捕获 `Exception` 的响应体保持步骤 1 的
`{"error": {"code": "internal_error", "request_id": "..."}}` 最小结构（步骤 1 单测对此做精确断言）；
`cause` 只出现在日志的 `cause_type` 字段中。显式抛出的 `AgentError(internal_error)` 仍走完整五字段结构。

**行为变化**：

- 步骤 1 的 `HTTPException` 处理结果为 `code=http_error`，本步骤改为 `http_{status}`（按 4.4.1 要求）；
- 参数校验错误由框架默认的 **422 + `{"detail": [...]}`** 改为 **400 + 统一错误体**；
- 正常业务响应（`/chat`、`/chat/stream`、`/webhook`、静态页）结构不变。

### 4.2 工具层包装（`src/errors/wrappers.py`）

沿用步骤 1 的 monkey-patch，在 `tools.*` 与 `agent` 模块同名引用外层包一层；**与步骤 1 的 `tool_call` 埋点复用同一次 wrap**（`wrap_tool` 内 `finally` 记录 `tool_call`），不存在双层包装（`_obs_wrapped` 幂等标记）。

原始异常 → 领域异常映射：

| 原始异常 | 映射结果 | retryable |
| --- | --- | --- |
| `AgentError` 及其子类 | 原样抛出 | 由原异常决定 |
| `json.JSONDecodeError` / `KeyError` / `FileNotFoundError` | `ToolExecutionError`（含 `tool` / `args_keys`） | 默认 `true` |
| `httpx.HTTPError` 子类，或模块名为 `httpx*` / `requests*`、类名属 HTTP/Timeout 家族 | `DataSourceError`（含 `tool`） | 默认 `true` |
| 其他 `Exception`（如 `ValueError`、`TypeError`） | `ToolExecutionError`（含 `tool` / `args_keys`） | 默认 `true` |

- 工具正常返回 `{"found": false}` 等业务失败**不是异常**，不改写为错误；
- wrapper 用 `functools.wraps`，保留原函数签名、返回值与名称；
- `args_keys` 通过 `inspect.signature.bind_partial` 获取，只记键名。

### 4.3 LLM 层包装（`src/errors/wrappers.py`）

包装对象是 `bot.client.chat.completions.create` 的实例属性；**与步骤 1 的 `llm_call` 埋点复用同一次 wrap**，先记 `llm_call(start)`，成功后记 `llm_call(end, ok=true)`，失败记 `llm_call(end, ok=false)` 再抛领域异常。

| 原始异常 | 映射结果 | retryable 依据 |
| --- | --- | --- |
| `TimeoutError`、类名含 `Timeout`（覆盖 `openai.APITimeoutError`、`httpx.TimeoutException`） | `LLMError(llm_failed)` | `true`（超时） |
| HTTP 状态 4xx（429 除外） | `LLMError(llm_failed)` | `false`（参数/鉴权类，重试无意义） |
| HTTP 状态 429 / 5xx | `LLMError(llm_failed)` | `true` |
| 其他异常 | `LLMError(llm_failed)` | `true` |

- 状态码从 `exc.status_code` 或 `exc.response.status_code` 读取，不依赖 `openai` 具体类；
- Mock 模式 `getattr(bot, "client", None) is None` → `instrument_llm` 返回 `False`，不报错；
- `stream=True` 的耗时口径限制沿用步骤 1 notes 第 10 节：只覆盖 `create()` 返回流对象的时间。

## 5. 兜底接入点（`src/errors/fallback.py`）

```text
FallbackHook(Protocol)
    async def handle(error: AgentError, context: dict) -> Optional[AgentError]

NoOpFallbackHook   # 默认实现：记录 error_raised 后原样返回
register_hook(hook) / get_hook()
```

- `context` 固定包含 `request_id`、`path`、`session_id`（可得时）、`tool`（工具层异常时）；
- 调用点：`handlers.py` 的 `AgentError` / 参数校验 / `HTTPException` / 未捕获异常四个处理器，均先 `await get_hook().handle(...)`，再用返回的错误生成响应；
- 默认实现只做“记录 + 原样返回”，**不含任何重试、退避、降级**；
- 阶段三替换 `register_hook(...)` 的实现即可接入 Retry，处理器与调用点无需改动；
- 测试通过 `register_hook` 注入记录型 hook，验证 `handle` 被调用一次。

## 6. 流式响应限制

- `/chat/stream` 在**流开始前**抛出的 `AgentError`（例如 `store.get` 或 `bot.reply_stream` 首次产出前失败）仍走全局处理器，返回统一错误体；
- **流开始后**的异常：中间件记录 `request_error`，响应头已发出、无法改写 body，因此**不在 NDJSON 中追加 `error` 事件**，仅终止流；这是本步骤的明确限制；
- NDJSON 的 `error` 事件协议设计留给**步骤 4（Harness Tracing / 路由透出）**统一处理，届时在流式协议层补充；
- 步骤 1 的“末行追加 `{"event":"done","request_id":"..."}`”行为在本步骤保持不变，未做改动。

## 7. vendor 改动

无。所有能力均通过运行期包装实现：

- `src/main.py` 仅导入 vendor 的 `app` / `agent` / `tools` 并注册中间件与处理器；
- 工具与 LLM 异常分类都在 `src/errors/wrappers.py` 的 wrapper 内完成，未修改 vendor 的 `app.py` / `agent.py` / `tools.py` / `datasource.py`；
- 已确认 `vendor/ecommerce-customer-service-agent` 的 `git status` 为空。

## 8. 测试

```text
pytest tests/test_errors.py tests/test_request_id.py -q
14 passed in 0.65s
```

`tests/test_errors.py`（8 项）覆盖：

| 用例 | 断言 |
| --- | --- |
| 工具 `ValueError` | 500、`tool_failed`、`retryable=true`、`details.tool=query_order`、`error_raised.cause_type=ValueError` |
| 工具 `FileNotFoundError` | `tool_failed`、`details.args_keys=["order_id"]` |
| LLM `TimeoutError` | 502、`llm_failed`、`retryable=true` |
| LLM 400 | 502、`llm_failed`、`retryable=false` |
| `/chat` 缺 `message` | 400、`validation_error`、`retryable=false`、`details.fields=["message"]` |
| 未捕获 `RuntimeError` | 500、`internal_error`、body 不含 `cause` 与原始异常文本 |
| `register_hook` 注入测试 hook | `handle` 恰好调用一次，`context` 含 `request_id`/`path`/`tool` |
| Mock 模式正常请求 | 200、无 `error_raised` |

步骤 1 的 `tests/test_request_id.py`（6 项）全部回归通过，未修改其断言。

## 9. 运行时验证（`uvicorn` + curl）

| 场景 | 实际响应 |
| --- | --- |
| 工具抛 `ValueError` | 500 `tool_failed`，`retryable=true`，`details={"tool":"query_order","args_keys":["order_id"]}` |
| LLM 抛 `TimeoutError` | 502 `llm_failed`，`retryable=true`，`details={}` |
| `/chat` 缺 `message` | 400 `validation_error`，`retryable=false`，`details={"fields":["message"]}` |
| 未捕获 `RuntimeError` | 500 `internal_error`（最小兼容结构） |
| 正常 `/chat` | 200，`{"reply": ...}` |

所有响应的 `X-Request-ID` 与 body `request_id` 一致；日志中四类错误均产生 `error_raised`，且 `request_id` 与响应头一致。

示例日志：

```json
{"ts":"...","level":"INFO","service":"ecommerce-agent","request_id":"rt-tool-00000001","event":"tool_call","tool":"query_order","args_keys":["order_id"],"ok":false,"latency_ms":0.06,"error_type":"ValueError"}
{"ts":"...","level":"INFO","service":"ecommerce-agent","request_id":"rt-tool-00000001","event":"error_raised","code":"tool_failed","retryable":true,"cause_type":"ValueError","path":"/__tmp_tool","tool":"query_order"}
{"ts":"...","level":"INFO","service":"ecommerce-agent","request_id":"rt-llm-00000001","event":"error_raised","code":"llm_failed","retryable":true,"cause_type":"TimeoutError","path":"/__tmp_llm"}
```

## 10. 遗留问题

1. **流式错误协议**：流开始后的异常无法改写 body，也不追加 `error` 事件，留给步骤 4 统一设计。
2. **`tool_failed` 默认 `retryable=true`**：为满足本步骤用例；但 `KeyError`/`FileNotFoundError` 这类确定性错误重试收益低，阶段三可按 `cause_type` 细化。
3. **`internal_error` 响应体为步骤 1 的最小结构**：为兼容既有单测与契约；其余错误码均为五字段统一结构。若后续放宽步骤 1 断言，可统一为五字段。
4. **HTTPException 响应体变化**：由 `{"detail": ...}` 改为统一错误体，`code` 为 `http_{status}`；依赖旧结构的调用方需同步调整。
5. **`llm_call` 的 `stream=True` 耗时口径**：仍只覆盖流对象创建，不覆盖逐块消费（同步骤 1）。
6. **`/webhook/{platform}` 的 `session_id`**：仍来自 `channels.normalize()`，未进入日志上下文；工具异常时 `error_raised.session_id` 为空（与步骤 1 一致）。
7. **`datasource_failed` 的识别基于类名/模块名**：未引入对具体 SDK 的强依赖；若后续接入自定义数据源异常，需要补充映射表。
8. **`RateLimited` 仅保留错误码**：限流命中逻辑在步骤 3 实现，本步骤无任何限流行为。

## 11. 复现命令

```bash
# 运行测试
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pytest tests/test_errors.py tests/test_request_id.py -q

# 启动服务
bash scripts/dev_run.sh

# 触发参数校验错误（400 validation_error）
curl -s -D - -X POST http://127.0.0.1:8010/chat \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: demo-validation-01" \
  -d '{"session_id":"s1"}'

# 触发工具异常（重命名数据文件后请求订单查询，再恢复）
# mv vendor/ecommerce-customer-service-agent/data/orders.json /tmp/orders.json.bak
# curl -s -X POST http://127.0.0.1:8010/chat -H "Content-Type: application/json" \
#   -d '{"session_id":"s1","message":"查订单 20261006001"}'
# mv /tmp/orders.json.bak vendor/ecommerce-customer-service-agent/data/orders.json
```

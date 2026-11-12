# 阶段二 · 步骤 4 实现说明：NDJSON 流式协议字段预留

> 在步骤 1–3 之上，为 `/chat/stream` 定义可演进的事件 schema、StreamEmitter 与 schema 自查端点，
> 并把“流开始后异常”从“仅记录日志”升级为协议内 `error` 事件。本步骤只预留字段与接入点，
> 不填充路由、工具轨迹、用量与缓存业务数据。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 协议版本 | `STREAM_PROTOCOL_VERSION = "1"` |
| 事件类型 | `start` / `route` / `tool_call` / `usage` / `cache` / `error` / `done` / `delta`（8 类） |
| 新增端点 | `GET /chat/stream/schema` |
| vendor 是否被改动 | **否**（`git status` 为空）；通过替换 `APIRoute.endpoint` / `dependant.call` 接入 |
| 新增文件 | `src/stream/{schema,emitter,middleware_bridge}.py`、`src/routes/stream_schema.py`、3 个测试文件 |
| 修改文件 | `src/main.py`、`src/obs/request_id.py`（done 去重）、`tests/test_request_id.py`（流式断言适配） |
| 测试 | `pytest tests/` → **40 passed**（既有 26 项 + 新增 14 项） |

## 2. 事件 schema

| 事件 | 必填字段 | 可选/预留字段 | 说明 |
| --- | --- | --- | --- |
| `start` | `event`, `request_id`, `ts`, `seq`, `session_id` | `protocol_version`, `trace_id`, `span_id` | 新增；`protocol_version` 恒为 `"1"` |
| `route` | `event`, `request_id`, `ts`, `seq` | `agent`, `confidence`, `reason`, `trace_id`, `span_id` | 阶段四填充 |
| `tool_call` | `event`, `request_id`, `ts`, `seq` | `tool`, `ok`, `latency_ms`, `args_keys`, `trace_id`, `span_id` | 阶段三填充；`args_keys` 只记参数名 |
| `usage` | `event`, `request_id`, `ts`, `seq` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `trace_id`, `span_id` | 阶段三填充 |
| `cache` | `event`, `request_id`, `ts`, `seq` | `hit`, `layer`（`session`/`semantic`）, `key_prefix`, `trace_id`, `span_id` | 阶段五填充 |
| `error` | `event`, `request_id`, `ts`, `seq`, `code`, `message`, `retryable` | `details`, `trace_id`, `span_id` | 本步骤承接；与步骤 2 的 error.payload 对齐 |
| `done` | `event`, `request_id`, `ts`, `seq` | `status`（`ok`/`failed`）, `latency_ms`, `usage`, `route`, `cache`, `trace_id`, `span_id` | 旧 `{"done": true}` 保留，本事件为扩展元数据 |
| `delta` | `delta` | — | 完全原样，不注入任何新字段 |

- schema 由 `src/stream/schema.py` 的 `list_event_schema()` 单一来源维护，端点与 emitter 共用；
- `ts` 为毫秒时间戳；`seq` 从 0 开始，每个事件自增 1；
- `trace_id` / `span_id` 缺省时**不输出该字段**，避免空值污染 JSON。

### 2.1 协议版本策略

- `start` 事件携带 `protocol_version`，前端可据此做兼容判断；
- 新增可选字段不递增版本；新增必填字段、修改既有字段语义时递增；
- 本步骤为 `"1"`，后续步骤 5（Langfuse 映射）只增加可选字段，不改变版本。

## 3. StreamEmitter

文件：`src/stream/emitter.py`。

- 构造：`StreamEmitter(request_id=None, session_id=None, trace_id=None, span_id=None)`；
  `request_id` 为空时从步骤 1 的 contextvars 读取并在构造期固化（生成器迭代期间不再读取）；
- 方法：`emit_start` / `emit_route` / `emit_tool_call` / `emit_usage` / `emit_cache` / `emit_error` / `emit_done`；
- `seq` 单调递增；`emit_done` 后实例关闭，再调用任何 `emit_*` 抛 `RuntimeError`；
- 异常路径允许 `emit_error` → `emit_done(status="failed")`；
- `emit_error` 复用 `AgentError.to_payload()`，只摊平 `code` / `message` / `retryable` / `details`，
  **不输出 `cause`、堆栈或原始异常文本**；
- `emit_done` 会回填此前发射过的 `route` / `usage` / `cache` **摘要**（不重复完整业务数据）；
- 本步骤不主动发射 `route` / `tool_call` / `usage` / `cache`，只保证方法与格式可用。

### 3.1 生成器包装

- 同步版 `wrap_streaming_generator(gen, emitter)` 供单元测试与同步迭代器使用；
- 异步版 `wrap_async_streaming_generator(gen, emitter)` 供运行时使用：
  Starlette 1.x 的 `StreamingResponse` 只接受异步 `body_iterator`，vendor 的同步生成器在构造时
  已被包成线程池异步迭代器，因此桥接层按 `__aiter__` 分支选择版本；
- 两版行为一致：先发 `start` → 原样透传 vendor 行 → 正常结束补扩展 `done(status="ok")`；
- 流内 `AgentError` → `error` + `done(failed)`；非 `AgentError` → 包装为 `internal_error` 后同样下发；
- 若生成器自带扩展 `done`，则不再追加，避免重复。

## 4. `/chat/stream` 接入方式

为保持 vendor 零改动，`src/stream/middleware_bridge.py` 的 `install_stream_protocol(app)` 在启动时：

1. 遍历 `app.routes`，找到 `path == "/chat/stream"` 的 `APIRoute`；
2. 用 `functools.wraps` 包装原端点：先调用原端点拿到 `StreamingResponse`，再替换其 `body_iterator`；
3. 同步替换 `route.endpoint` 与 `route.dependant.call`（FastAPI 实际调用的是 `dependant.call`）；
4. 以 `_stream_protocol_wrapped` 标记保证幂等。

OpenAPI 描述未受影响：`dependant` 在路由注册时已构建，`/openapi.json` 仍按原端点生成；
本步骤未启动服务对比 OpenAPI，但未修改 `dependant` 的签名、参数模型与响应模型。

### 4.1 成功路径实测输出

```text
{"event":"start","request_id":"st4-stream-0001","ts":1794278953201,"seq":0,"session_id":"st4","protocol_version":"1"}
{"delta":"抱歉，没有查到该订单，请核对订单号是否正确。"}
{"done":true}
{"event":"done","request_id":"st4-stream-0001","ts":1794278953209,"seq":1,"status":"ok","latency_ms":7.67}
```

`event=done` 计数为 1，`{"delta": ...}` 与 `{"done": true}` 逐字节保持不变。

### 4.2 步骤 1 done 行的去重（对 `request_id` 中间件的必要修改）

步骤 1 的 `request_id` 中间件会在流末尾**无条件**追加一条简版
`{"event":"done","request_id":"..."}` 到 ASGI 最后一个空 body 块。步骤 4 起 StreamEmitter 已发射
扩展 `done`，若不处理会出现两个 done 事件。

因此在 `src/obs/request_id.py` 增加了最小改动：

- 新增 `_chunk_has_done_event()`，判断已发送的 body 块最后一行是否为 `event == "done"`；
- 记录 `saw_stream_done_event`；已出现扩展 done 时跳过简版追加；
- 未包装的流（例如未来直接请求 vendor app）仍保持步骤 1 的原有行为。

对应地，`tests/test_request_id.py` 的流式用例做了**协议适配**：由“最后一行等于简版 done”改为
“存在且仅存在一个 done 事件、`status=ok`、首行为 start”。这是步骤 4 协议扩展的预期变化，
其余 5 项步骤 1 测试未改动。

## 5. 流开始后异常承接

- 复用 4.1 的包装逻辑：`delta` 之后抛 `AgentError` → 下发 `error` + `done(failed)`；
- 非 `AgentError` → 包装为 `AgentError(internal_error, cause=exc)` 后下发；
- **HTTP 状态码保持 200**：响应头在首个 chunk 时已发出，之后无法改写状态码（技术限制），
  因此协议内用 `error` 事件表达失败，前端应以 `error` 或 `done.status=failed` 判定；
- `details` 仍遵循步骤 2 的“仅安全字段”约束；`cause` 只存在于进程内对象，不进入流、不进入响应体。

实测（monkeypatch 注入 `LLMError`）：

```text
STATUS 200 rid=st4-error-000001
{"event":"start",...}
{"delta":"部分内容"}
{"event":"error","request_id":"st4-error-000001","ts":...,"seq":1,"code":"llm_failed","message":"模型服务调用失败","retryable":true,"details":{}}
{"event":"done","request_id":"st4-error-000001","ts":...,"seq":2,"status":"failed"}
```

响应文本中不含 `runtime stream timeout`、`Traceback`、`cause`。

## 6. schema 自查端点

- `GET /chat/stream/schema` → `200`，body 为：
  `{"protocol_version": "1", "events": {事件名: {required, optional, compatibility}}}`；
- 经过 request_id 中间件，响应头带 `X-Request-ID`；
- 记录 `stream_schema_fetched` 事件（复用 `log_event`，不新增日志字段）；
- 未加入步骤 3 的限流豁免（不修改既有配置），默认参与限流；如长期公开建议运维将其加入
  `RATE_LIMIT_EXCLUDE_PATHS`。

## 7. Langfuse 映射预留（步骤 5）

- 所有事件预留 `trace_id` / `span_id` 可选字段，`StreamEmitter` 支持构造时注入；
- 本步骤**未引入**任何 Langfuse SDK 或依赖；
- 阶段五接入时，只需在构造 emitter 前生成/透传 trace 上下文，事件结构无需变更。

## 8. 前端兼容性

- `static/index.html` 未修改；
- 成功流中 `{"delta": ...}` 与 `{"done": true}` 原样保留，新增事件行不含 `delta`、`done=true`，
  旧解析器会自然忽略；
- 单元测试 `test_frontend_compatibility_parser` 以与前端相同的口径（只消费 delta、遇到 done 停止）
  解析包装后的流，得到完整文本与结束标记。

## 9. vendor 改动

无。`app.py` / `agent.py` / `tools.py` / `static/index.html` 均未修改；
`/chat/stream` 的接入完全通过运行期替换路由端点完成，已在 4 节记录实现方式。

## 10. 测试

```text
pytest tests/ -q
40 passed in 0.66s
```

新增 14 项：

- `tests/test_stream_schema.py`（1）：版本、8 类事件、必填/可选字段、响应头与日志事件；
- `tests/test_stream_emitter.py`（12）：各事件字段、可选字段省略、seq 单调、关闭保护、
  error 摊平不含 cause、trace/span 注入、done 回填摘要、成功/失败/非 AgentError 包装顺序、
  done 去重、前端解析口径；
- `tests/test_stream_error_event.py`（1）：真实 `/chat/stream` 流中异常 → error + done(failed)、
  状态码 200、request_id 一致、无堆栈泄露。

回归：步骤 1–3 的 26 项全部通过（其中流式用例按 4.2 节说明做了协议适配）。

## 11. 遗留问题

1. **`route` / `tool_call` / `usage` / `cache` 目前不发射**：仅预留给阶段三/四/五填充。
2. **`/chat/stream/schema` 默认参与限流**：高频读取会占用 IP 配额，运维可按需加入豁免。
3. **流内非 AgentError 统一为 `internal_error`**：暂不细分错误码，待步骤 5/阶段三补充。
4. **流内错误不产生 `error_raised` 日志**：本步骤只下发协议事件；若需要日志侧统计，可在后续补充
   `stream_error` 事件（避免与步骤 2 的 handler 日志重复）。
5. **`session_id` 在 emitter 构造时固化**：若请求体缺少 `session_id`，start 事件为空串。
6. **OpenAPI 未做快照对比**：仅确认未修改 `dependant` 的签名/模型；如需严格保证可补充快照测试。
7. **`seq` 仅保证单请求内单调**：跨请求不共享，也不承担全局排序职责。

## 12. 复现命令

```bash
# 全部测试
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pytest tests/ -q

# 启动
bash scripts/dev_run.sh

# 协议自查
curl -s http://127.0.0.1:8010/chat/stream/schema | python -m json.tool

# 流式输出（应看到 start → delta → {"done":true} → 扩展 done）
curl -sN -X POST http://127.0.0.1:8010/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s4","message":"查订单 20261006001"}'
```

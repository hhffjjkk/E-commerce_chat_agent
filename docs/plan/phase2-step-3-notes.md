# 阶段二 · 步骤 3 实现说明：健康检查与基础运行防护

> 在步骤 1（request_id / JSON 日志）与步骤 2（错误码 / 统一错误响应）之上，补齐健康检查、CORS、
> 进程内 IP 限流与请求体大小限制。全部实现位于 `src/`，vendor 零改动。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| vendor 是否被改动 | **否**（`vendor/ecommerce-customer-service-agent` 下 `git status` 为空） |
| 新增依赖 | 无（Redis 仅为就绪检查按需延迟导入，缺失时降级） |
| 新增文件 | `src/runtime/{health,cors,rate_limit,body_limit,responses}.py`、3 个测试文件 |
| 修改文件 | `src/config.py`（新增配置）、`src/obs/events.py`（新增 3 个事件）、`src/main.py`（注册） |
| 新增端点 | `GET /health`、`GET /health/ready` |
| 测试 | `pytest tests/` → **26 passed**（既有 14 项 + 本步骤 12 项） |

## 2. 新增端点

### 2.1 `GET /health`（存活检查）

- 不访问 Redis / LLM / 数据源；
- `200` + `{"status": "ok", "service": "<SERVICE_NAME>", "request_id": "<id>"}`；
- 经过 request_id 中间件，响应头带 `X-Request-ID`；
- 日志：`health_check`，`kind=liveness`，`status=ok`。

### 2.2 `GET /health/ready`（就绪检查）

| 检查项 | 行为 | detail 取值 |
| --- | --- | --- |
| `redis` | `REDIS_URL` 已配置时 `PING`（连接/读取超时各 1s）；未配置不检查 | `pong` / `not_configured` / `timeout` / `unreachable` |
| `data` | 仅检查 `data/*.json` 可读；`ApiDataSource` 模式不发请求 | `N files readable` / `N files unreadable` / `data dir missing` |

状态与状态码：

- 全部 ok → `status=ok`、`200`；
- 数据源失败 → `status=down`、`503`（核心依赖不可用）；
- 数据源正常但 Redis 失败 → `status=degraded`、`503`（缓存属可选依赖）；
- 日志：`health_check`，`kind=readiness`，带完整 `checks`。

实测（无 Redis）：

```json
{"status":"ok","checks":{"redis":{"ok":true,"detail":"not_configured"},"data":{"ok":true,"detail":"4 files readable"}},"service":"ecommerce-agent","request_id":"st3-ready-00001"}
```

实测（`REDIS_URL` 指向不可达端口）：`503`，`{"status":"degraded","checks":{"redis":{"ok":false,"detail":"timeout"},...}}`。

> 注意：就绪检查失败**不会**改变业务路由行为。若配置了不可达的 `REDIS_URL`，`memory.create_store()`
> 仍会选择 Redis 会话存储，`/chat` 会在业务层报连接错误（本步骤不负责降级，属阶段三/五范畴）。

## 3. 中间件注册顺序

FastAPI/Starlette 的 `add_middleware` 是**逆序**的：后注册的更靠外。`src/main.py` 的实际注册顺序为：

```python
configure_cors(app, settings)                  # 1. 先注册 -> 最内（仅配置非空时）
app.add_middleware(BodyLimitMiddleware)        # 2. 其次
app.add_middleware(RateLimitMiddleware)        # 3. 再次
app.add_middleware(RequestContextMiddleware)   # 4. 最后 -> 最外
```

实际调用顺序（外 → 内）：

```text
request_id -> rate_limit -> body_limit -> cors -> ExceptionMiddleware -> 业务路由
```

验证依据（运行时）：

- 限流 429 响应带 `X-Request-ID`，说明 `request_id` 在 `rate_limit` 之外；
- 请求体超限 400 响应同样带 `X-Request-ID`，说明 `body_limit` 位于 `request_id` 之内；
- 正常 `POST /chat` 带 `Access-Control-Allow-Origin`，说明 CORS 覆盖业务响应。

### 3.1 限流/请求体中间件如何返回统一错误

`rate_limit` 与 `body_limit` 是纯 ASGI 中间件，位于 `ExceptionMiddleware` **之外**；直接 `raise AgentError`
会继续冒泡到 `ServerErrorMiddleware`，被当作未捕获异常包成 `internal_error`。因此两者复用同一个全局处理器函数：

```python
# src/runtime/responses.py
response = await handle_agent_error(Request(scope), error)  # 同一套 hook / payload / request_id
response.headers.update(extra_headers)                      # Retry-After / CORS
await response(scope, receive, send)
```

行为等价于“抛出 → 全局处理器转换”，且仍会触发 `FallbackHook.handle()` 与 `error_raised` 日志。
这是实现路径上的必要说明，未修改步骤 2 的任何既有逻辑。

### 3.2 429 的 CORS 头

CORS 中间件在限流**内侧**，无法为限流提前返回的响应补头。因此 `rate_limit` 在返回 429 时，会按
`CORS_ALLOW_ORIGINS` 手工补 `Access-Control-Allow-Origin`（仅当请求 `Origin` 命中白名单），
并加 `Vary: Origin`；不回写 `Access-Control-Allow-Credentials`（固定无凭证）。

已验证：CORS 未配置时 429 无 CORS 头；配置后命中来源的 429 带 `Access-Control-Allow-Origin`。

## 4. 配置项（`src/config.py`）

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CORS_ALLOW_ORIGINS` | 空字符串 | 逗号分隔；空表示不注册 CORS |
| `RATE_LIMIT_ENABLED` | `true` | 限流总开关（`1/true/yes/on` 为真） |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | 固定窗口秒数（浮点，>0） |
| `RATE_LIMIT_MAX_REQUESTS` | `120` | 每窗口每 IP 最大请求数（>0） |
| `RATE_LIMIT_EXCLUDE_PATHS` | `/health,/health/ready` | 豁免路径，逗号分隔 |
| `MAX_BODY_BYTES` | `65536` | `Content-Length` 上限（>0） |

- 非法或缺失值回落到默认值；
- 启动时输出一条 `config_loaded` 事件（仅生效值与数量，不含 `REDIS_URL` 等敏感值）：

```json
{"event":"config_loaded","cors_enabled":false,"cors_origins_count":0,"rate_limit_enabled":true,
 "rate_limit_window_seconds":60.0,"rate_limit_max_requests":2,
 "rate_limit_exclude_paths":["/health","/health/ready"],"max_body_bytes":1024}
```

- 项目未新增 `.env` 文件；vendor 内 `.env` 未修改。新配置项通过进程环境变量注入（`dev_run.sh` 已透传），
  例如：`RATE_LIMIT_MAX_REQUESTS=60 CORS_ALLOW_ORIGINS=https://shop.example.com bash scripts/dev_run.sh`。

## 5. 限流

### 5.1 算法与口径

- **固定窗口计数器**：`dict[str, deque[float]]`，每 IP 一个时间戳队列；
- 每次请求剔除当前 IP 队列中超出窗口的时间戳（不启动后台清理任务）；
- 队列长度 ≥ 阈值 → 429；否则追加当前时间戳；
- 进程内计数（`threading.Lock` 保护），**重启即清零；多实例各自独立**；
- 客户端 IP：优先 `X-Forwarded-For` 首个地址，否则 `request.client.host`；**不读取 `X-Real-IP`**；
- `OPTIONS` 预检不计数、不拦截；
- `/health`、`/health/ready` 默认豁免（可配置覆盖）；
- 429 响应：步骤 2 统一结构 + `Retry-After: <剩余秒数>`（向上取整，最小 1）。

实测（`max=2`、窗口 60s）：

```text
POST /chat -> 200
POST /chat -> 200
POST /chat -> 429, Retry-After=60
{"error":{"code":"rate_limited","message":"请求过于频繁，请稍后再试",
          "request_id":"st3-rate-000002","retryable":true,"details":{}}}
```

日志：

```json
{"event":"rate_limited","ip":"127.0.0.1","path":"/chat","limit":2,"window":60.0,
 "request_id":"st3-rate-000002"}
```

### 5.2 多实例限制

进程内字典随进程生命周期存在：重启归零，多实例之间不共享。阶段五引入 Redis 双层缓存时，可将计数键
（如 `rl:{ip}:{window_start}`）迁移到 Redis 并保留同一中间件接口。本步骤不写 Redis。

### 5.3 `X-Forwarded-For` 信任策略

当前实现无条件信任 `X-Forwarded-For` 的第一个地址，仅适用于**可信反向代理（Nginx/网关）已覆盖该头**的部署。
若服务直接暴露公网，客户端可伪造该头绕过或定向限流；生产部署需由最外层代理清洗该头，或后续引入
`TRUSTED_PROXIES` 白名单后再决定是否采信。本步骤未实现代理白名单。

## 6. 请求体大小限制

- 仅对 `POST` / `PUT` / `PATCH` 生效；
- 读取 `Content-Length`，超过 `MAX_BODY_BYTES` → `400`、`code=validation_error`、`message=请求体过大`，
  `details={"max_bytes": ..., "content_length": ...}`；
- 不缓存、不改写请求体；
- **`Content-Length` 缺失（chunked / 流式上传）时不做截断**：本步骤只做头部检查，流式读取限制留给后续需要时实现；
  在此之前，chunked 请求体不受 `MAX_BODY_BYTES` 约束。

实测（`MAX_BODY_BYTES=1024`）：

```json
{"error":{"code":"validation_error","message":"请求体过大","request_id":"st3-body-000001",
          "retryable":false,"details":{"max_bytes":1024,"content_length":1235}}}
```

## 7. CORS

- `CORS_ALLOW_ORIGINS` 为空（默认）→ **不注册** `CORSMiddleware`，行为与步骤 2 完全一致；
- 非空时参数固定为：`allow_methods=["GET","POST","OPTIONS"]`、`allow_headers=["Content-Type","X-Request-ID"]`、
  `allow_credentials=False`、`max_age=600`；
- 默认关闭的理由：跨域授权应显式配置，避免默认 `*` 放宽带来的安全面扩大；且当前前端与 API 同源部署无需 CORS；
- 配置含 `*` 时输出 `cors_warning` 事件，但允许通过（`allow_credentials` 已固定 `False`，规避 `* + credentials` 风险）；
- 未命中白名单的 `Origin` 不返回 `Access-Control-Allow-Origin`。

实测：

```text
CORS 未配置：OPTIONS /chat -> 405，无 Access-Control-Allow-Origin
CORS=http://example.com：POST /chat (Origin 匹配) -> 200，ACAO=http://example.com，无 credentials
CORS=http://example.com：POST /chat (Origin 不匹配) -> 200，无 ACAO
预检 OPTIONS /chat -> 200，ACAO=http://example.com，Allow-Methods=GET, POST, OPTIONS
```

**已知限制**：未捕获异常（500 `internal_error`）由最外层的 `ServerErrorMiddleware` 直接生成，
不经过内侧 CORS 中间件，因此这类响应不带 CORS 头；步骤 2 统一处理的领域错误（400/429/502 等）
正常携带 CORS 头。

## 8. vendor 改动

无。`app.py` / `agent.py` / `tools.py` / `datasource.py` / `memory.py` 均未修改；健康检查通过延迟导入
`datasource.DATA_DIR` 读取 vendor 数据目录，只读不写。

## 9. 测试

```text
pytest tests/ -q
26 passed in 0.78s
```

新增 12 项：

- `tests/test_health.py`（3）：`/health` 结构与日志、`/health/ready` 无 Redis 时 200、数据目录不可读时 503 `down`；
- `tests/test_cors.py`（4）：默认不注册、空配置不注册、白名单来源返回 ACAO 且无凭证、非白名单来源不返回；
- `tests/test_rate_limit.py`（5）：第 3 次 429 + `Retry-After` + 日志、健康路径豁免、OPTIONS 不计数、
  总开关关闭、请求体超限 400。

步骤 1、2 的 14 项测试全部回归通过，未修改其断言。

## 10. 遗留问题

1. **限流为进程内计数**：重启清零、多实例不共享；阶段五迁移到 Redis。
2. **`X-Forwarded-For` 无条件信任**：需可信代理清洗或后续增加代理白名单。
3. **`Content-Length` 缺失时无上限**：chunked 请求不受 `MAX_BODY_BYTES` 约束，未实现流式截断。
4. **限流计数不区分路由**：同一 IP 的 `/chat` 与 `/webhook` 共用窗口额度（健康检查除外），
   如需按路由分桶可在后续扩展 key。
5. **空桶不回收**：仅清理当前 IP 的过期时间戳，长期大量不同 IP 会累积空队列；生产可加定期清理。
6. **500 响应无 CORS 头**：`ServerErrorMiddleware` 位于 CORS 之外，见 7 节说明。
7. **就绪检查与业务存储解耦**：`REDIS_URL` 不可达时 `/health/ready` 为 `degraded`，但 `/chat` 仍会因
   Redis 会话存储报错；降级/回退策略留待阶段三。
8. **健康检查不鉴权**：`/health`、`/health/ready` 对公网暴露，仅包含状态与计数，无敏感信息；
   如需限制访问需在网关层处理（本阶段不做鉴权）。

## 11. 复现命令

```bash
# 全部测试
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pytest tests/ -q

# 启动（默认配置）
bash scripts/dev_run.sh

# 健康检查
curl -s -D - http://127.0.0.1:8010/health
curl -s -D - http://127.0.0.1:8010/health/ready

# 调小限流阈值验证 429
RATE_LIMIT_MAX_REQUESTS=2 bash scripts/dev_run.sh
for i in 1 2 3; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8010/chat \
    -H "Content-Type: application/json" -d '{"session_id":"s1","message":"hi"}'
done

# 请求体限制（MAX_BODY_BYTES=1024）
MAX_BODY_BYTES=1024 bash scripts/dev_run.sh

# CORS
CORS_ALLOW_ORIGINS=http://example.com bash scripts/dev_run.sh
curl -s -D - -X POST http://127.0.0.1:8010/chat \
  -H "Origin: http://example.com" -H "Content-Type: application/json" \
  -d '{"session_id":"s1","message":"hi"}'
```

# 阶段五步骤 1：Redis 客户端治理与降级策略

本步骤只做基础设施：统一 Redis 客户端、连接池参数、PING 缓存与显式降级。
**未实现**任何缓存语义（会话状态缓存 / pending store 迁 Redis / 语义缓存均留给后续步骤）。

## 1. 改动前基线（读代码记录）

| 项目 | 改动前实现 |
|---|---|
| `RedisSessionStore.__init__` | `redis.from_url(redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)`；无超时、无连接池上限、无健康检查间隔；每个实例各自建客户端 |
| `runtime/health.py::check_redis` | 每次探针都 `redis.from_url(..., socket_connect_timeout=1, socket_timeout=1, decode_responses=True)`，`PING` 后在 `finally` 关闭；失败返回 `timeout` / `unreachable` |
| `create_store` 调用点 | `app.py` 模块导入时一次（`store = create_store()`）；`agent_system/demo.py` 的 `main()` 一次；测试 fixture 多直接替换 `app_module.store`，不调用 `create_store` |
| 既有测试依赖 | 没有 `test_memory.py`；`test_health.py` 只覆盖“未配置 Redis → not_configured”，不依赖“每次新建连接”的行为 |
| 依赖版本 | `requirements.txt`: `redis>=5.0.0`，支持 `health_check_interval` 与 `from_url(decode_responses=...)` |

## 2. 方案选择

### 2.1 为什么集中管理客户端

- 改动前 `memory.py` 与 `health.py` 各写一套 `from_url`，超时/解码方式容易漂移；
- 阶段五后续的会话状态缓存、pending store、语义缓存都需要同一个连接池；
  若各自建客户端，Redis 连接数会随使用方数量线性增长；
- 统一后只有一个地方决定“超时多少、池多大、是否探活”，排障时也只有一个连接来源。

### 2.2 为什么默认“显式失败”而不是自动回退

`RedisManager.get_client()`：

- `REDIS_URL` 为空 → 返回 `None`，使用方走 SQLite/内存；
- `REDIS_URL` 已配置但不可达 → **抛 `RedisUnavailable`**，不自动回退。

理由：

1. 自动回退会造成“Redis 短时不可达 → 悄悄写 SQLite → 会话数据分散两份”的一致性灾难；
2. 显式失败让故障在 `/health/ready` 与 `redis_ping_failed` 日志里**看得见**；
3. 回退是策略，不是基础设施的隐含行为，必须由使用方显式选择。

### 2.3 `REDIS_FALLBACK_TO_MEMORY` 的适用场景

| 取值 | 行为 | 适用场景 |
|---|---|---|
| `false`（默认） | `create_store()` 抛 `RedisUnavailable`，进程启动失败 | 生产环境：宁可启动失败让编排系统重试/告警，也不接受会话数据分叉 |
| `true` | 回退 `SessionStore`（SQLite），记录 `redis_fallback` 与 `store_selected` | 本地演示、容灾演练：允许单机继续服务，但日志明确标注“正在降级” |

## 3. 连接池参数

| 参数 | 默认值 | 依据 / 调优建议 |
|---|---|---|
| `socket_timeout` | `1.0s` | 缓存数据可丢失、可重试；一次 Redis 抖动不应把用户请求拖到数秒。出现大量超时先查网络与 Redis 负载 |
| `socket_connect_timeout` | `1.0s` | 建连失败要快速暴露，避免启动或探针被长时间阻塞 |
| `max_connections` | `50` | 单实例同步调用足够；阶段六压测若出现 `ConnectionPool` 等待，再按并发度调大 |
| `health_check_interval` | `30s` | redis-py 内建空闲连接探活，避免拿到已被服务端断开的连接才报错；`0` 表示关闭 |
| `decode_responses` | `True` | 与既有 `RedisSessionStore` 的字符串语义一致，避免同一 key 在不同使用方出现 bytes/str 两种形态 |

## 4. PING 缓存与 `/health/ready`

- `ping(force=False)` 的缓存 TTL 默认 `REDIS_PING_CACHE_SECONDS=5`；
- 原因：会话缓存会在每个请求入口调用 `ping()`，若每次都真 PING，会把 Redis QPS 放大一倍；
  5 秒窗口在高并发下足够及时，在故障探测上也不迟钝；
- `/health/ready` 使用 `ping(force=True)`：健康检查要求实时性，**不接受 5 秒缓存窗口**，
  否则 Redis 刚挂掉时探针可能仍报 `ok`；
- `/health`（liveness）不访问 Redis，Redis 故障不影响 200；
- `/health/ready` 的响应结构保持不变：`status` / `checks.redis.ok` / `checks.redis.detail`；
  detail 由原来的 `timeout` / `unreachable` 细化为 `timeout` / `connect_error` / `ping_failed`。

## 5. `RedisUnavailable` 与 `AgentError` 的边界

- `AgentError` 是“面向用户可见”的领域异常，会被统一异常处理器映射为 HTTP 错误码；
- `RedisUnavailable` 是基础设施异常，**不并入 `AgentError`**：
  - memory 层可以选择回退 SQLite；
  - pending store 可以回退内存；
  - 语义缓存可以直接视为“未命中”；
- 如果并入 `AgentError`，上述使用方会被迫区分“基础设施故障还是业务错误”，
  并且可能把 Redis 故障直接暴露到 `/chat` 响应体；
- 异常消息只保留原因码（`not_configured` / `connect_error` / `timeout` / `ping_failed`），
  不含连接串或凭据。

## 6. 事件表与可观测性

| 事件 | 触发点 | 关键字段 |
|---|---|---|
| `redis_ping_failed` | `RedisManager.ping()` 真实 PING 失败（受 5 秒缓存约束） | `reason`（原因码） |
| `redis_fallback` | `create_store()` 在不可达且开启回退时 | `reason`、`fallback_to="sqlite"` |
| `store_selected` | `create_store()` 每次选择后端时 | `backend`（redis/sqlite）、`reason` |

所有事件都走 `log_event`，因此会进入 Langfuse 映射链路；阶段六可直接在
`obs/langfuse_mapping.py` 中把 `redis_*` 事件挂到 trace metadata，无需改本模块。

## 7. 与后续步骤的衔接

- **步骤 2（会话状态缓存）**：基于 `get_redis_manager().get_client()` 与 `ping()` 结果，
  实现 TTL、多租户前缀；缓存未命中或 Redis 不可用时按未命中处理；
- **步骤 3（pending store 迁 Redis）**：复用同一客户端，把 `InMemoryPendingStore` 的接口
  映射到 Redis Hash，并继续用 `RedisUnavailable` 表达不可用；
- **步骤 4（语义缓存）**：用同一客户端与 PING 结果决定“命中 / 跳过缓存”，
  不新增连接池。

## 8. 测试与验证

### 8.1 自动化测试

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_redis_manager.py` | 5 | 未启用、不可达（异常/缓存/事件）、可达与 force、连接参数、单例与 reset 关闭连接池 |
| `tests/test_memory_integration.py` | 4 | 默认 SQLite、Redis 可达与 key 格式、不可达不回退、显式回退与事件 |
| `tests/test_health_ready_redis.py` | 4 | not_configured、可达且每次 force PING、超时 detail、HTTP 503 degraded + /health 200 |

`pytest tests/`：**259 passed**（246 既有 + 13 新增）。
既有 246 项断言**零调整**：没有测试依赖 `redis.from_url` 的内部实现。

### 8.2 端到端验证

1. **真实 Redis 协议验证**：本机 Docker daemon 未运行，因此使用一个临时最小 RESP 服务
   （仅实现 HELLO/PING/RPUSH/LRANGE/LTRIM/DELETE，验证后已删除），设置
   `REDIS_URL=redis://127.0.0.1:6390/0`：

   ```text
   create_store() -> RedisSessionStore
   append/get 往返 -> [{'role': 'user', 'content': '你好'}, {'role': 'assistant', 'content': '您好'}]
   /health/ready -> 200 {"redis": {"ok": true, "detail": "pong"}}
   /chat 同 session 两轮 -> 200 / 200；Redis 中 chat:redis-s1 有 4 条消息
   ```

2. **不可达 + 默认不回退**：`REDIS_URL=redis://127.0.0.1:1` 启动：

   ```text
   redis_ping_failed reason=timeout
   agent_system.cache.errors.RedisUnavailable: timeout
   进程启动失败（退出码 1）
   ```

3. **不可达 + 显式回退**：`REDIS_FALLBACK_TO_MEMORY=true`：

   ```text
   redis_ping_failed / redis_fallback(fallback_to=sqlite) / store_selected(backend=sqlite)
   /health/ready -> 503 degraded，detail=timeout
   /health -> 200 ok
   /chat -> 200（SQLite 会话库正常）
   ```

## 9. 遗留

- 未引入 `fakeredis`（按要求）：单测使用 mock 客户端，真实连接验证使用临时 RESP 服务；
- 连接池参数目前是静态配置，未来可能需要按负载自适应；
- `RedisManager` 暂未暴露 metrics（连接数、PING 延迟、失败率），留待阶段六接入；
- `RedisSessionStore` 移除了原先可选的 `redis_url` 参数（无内部调用者），
  所有 Redis 使用方统一读取 `REDIS_URL`/`REDIS_*` 配置，避免多实例配置分裂；
- Docker daemon 未启动，未用真实 `redis:7` 容器复验；协议层已用临时 RESP 服务覆盖。

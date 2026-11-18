# 阶段五步骤 2：会话状态缓存与多租户 Key 隔离

本步骤在 `RedisManager` 之上落地两件事：
1. 历史消息支持多租户前缀与可配置 TTL；
2. 新增 `SessionStateCache`，旁路记录每会话最近一次路由。
**未**迁移 pending store、**未**实现语义缓存、**未**实现通用 profile 粘滞。

## 1. 改动前基线（读代码记录）

| 项目 | 改动前实现 |
|---|---|
| `RedisSessionStore.get` | `LRANGE chat:{sid} 0 -1`，解析 JSON，取末尾 `max_history` 条；无 TTL |
| `RedisSessionStore.append` | `RPUSH chat:{sid} <json>` + `LTRIM -max_history -1`；无 TTL |
| `RedisSessionStore.clear` | `DEL chat:{sid}` |
| Key 构造 | 三个方法各自内联 `f"chat:{session_id}"`，没有 `_key` 方法 |
| `create_store` | `RedisSessionStore(max_history)`，无 TTL 参数 |
| `/chat` 接入点 | `profile, decision, reason = select_profile(...)` 后直接 `bot.reply(...)`；`route` 由 `_route_payload` 生成 |
| `/chat/stream` 接入点 | `_stream_with_route()` 内 `select_profile(...)` 后立即 `emitter.emit_route(...)` |
| `store` 初始化 | `app.py` 模块导入时一次（`store = create_store()`），非 lifespan |
| `route` 字段 | `{agent, display_name, strategy, confidence, reason}`，对应 `_route_payload` |
| 既有测试对 key 的依赖 | 步骤 1 新增的 `test_memory_integration.py` 断言 `chat:s1`；前缀默认空时必须继续成立 |

## 2. Key 布局

| 用途 | Key 模板 | 结构 | 默认 TTL |
|---|---|---|---|
| 历史消息 | `{prefix}:chat:{session_id}` | LIST（JSON 字符串） | `REDIS_SESSION_TTL_SECONDS=0`（不过期） |
| 会话状态 | `{prefix}:session_state:{session_id}` | HASH | `REDIS_SESSION_STATE_TTL_SECONDS=1800`（30 分钟） |

实际形态：

| `REDIS_KEY_PREFIX` | 历史消息 | 会话状态 |
|---|---|---|
| 空（默认） | `chat:s1` | `session_state:s1` |
| `tenantA` | `tenantA:chat:s1` | `tenantA:session_state:s1` |

**为什么分成两个 key**：

- 生命周期不同：历史消息是可持久内容，会话状态是短周期元数据；
- 数据结构不同：消息是顺序列表，状态是字段集合；
- TTL 诉求不同：消息默认不过期，状态默认 30 分钟；
- 混在一个对象里会导致“只想让状态过期却把历史删掉”之类的耦合。

**为什么状态用 HASH 而不是 JSON 字符串**：HASH 支持 `HGET` 单字段读取、
`HINCRBY` 等原语，未来扩字段无需反序列化整个对象。

**为什么前缀默认为空**：现有部署与测试依赖 `chat:{sid}` 形态；空前缀保证向后兼容，
多租户部署再显式设置。允许前缀包含 `:` 是为了支持 `tenantA:prod` 这类分层命名。

非法前缀（含空格、`/` 等）在 `config.py` 读取时回退为空串，并记录
`config_invalid`（只记变量名，不记错误值）。

## 3. TTL 语义

| 对象 | 配置 | 默认 | 刷新时机 | 适用场景 |
|---|---|---|---|---|
| 历史消息 | `REDIS_SESSION_TTL_SECONDS` | 0（不过期） | `append` 时 `EXPIRE` | 默认保持持久化；需要控制内存时按业务留存期配置 |
| 会话状态 | `REDIS_SESSION_STATE_TTL_SECONDS` | 1800 秒 | `set_route` / `touch` 时 `EXPIRE` | 路由/活跃度是短周期数据，过期即可丢弃 |

- **读取不续期**：`get` 只读不 `EXPIRE`。只读会话不应永远驻留 Redis；
  若未来需要“读也续期”，应显式调用 touch 类方法，而不是隐式改变读语义。
- 历史消息默认不过期，是因为现有部署依赖历史消息持久化；开启 TTL 属于运维策略。

## 4. 降级语义对比

| 层 | Redis 未配置 | Redis 已配置但不可达 | 为什么不同 |
|---|---|---|---|
| `RedisManager.get_client` | 返回 `None` | 抛 `RedisUnavailable` | 会话存储不可静默分叉，必须显式失败（步骤 1 决策） |
| `SessionStateCache` | 全部 no-op，不记事件 | 读返回 `None`；写静默失败 + `session_state_write_failed` | 可丢失数据；缓存故障不应拖垮 `/chat` |
| `create_store` | SQLite | 默认抛错；`REDIS_FALLBACK_TO_MEMORY=true` 时回退 SQLite 并记 `redis_fallback` | 历史消息需要明确的存储归属 |

`SessionStateCache` 的所有方法**不抛 `RedisUnavailable`**：读失败等价于未命中，
写失败等价于丢弃；观测通过事件完成，而不是通过异常。

## 5. 隐私与日志字段

- `session_state_write_failed` 只记录 `session_id_hash`（`sha256(session_id)[:8]`）；
- 为避免 `log_event` 从请求上下文自动补齐原始 `session_id`，调用时显式传
  `session_id=None`，日志中该字段为 `null`；
- 不记录任何消息内容、路由理由原文或 LLM 输出；
- 成功事件 `session_state_updated` 默认不记：它是每请求一次的高频旁路事件，
  INFO 级别会造成日志膨胀；`SESSION_STATE_LOG_SUCCESS=true` 时以 DEBUG 级别输出。

## 6. 事件表

| 事件 | 触发点 | 字段 |
|---|---|---|
| `session_state_write_failed` | `set_route` / `touch` / `delete` 写失败（含 Redis 不可达） | `session_id_hash`、`op`、`reason` |
| `session_state_updated`（可选） | 写成功且 `SESSION_STATE_LOG_SUCCESS=true` | `session_id_hash`、`agent`、`strategy`、`has_confidence` |
| `config_invalid` | 任意非法配置（含非法 key 前缀） | `invalid_env`（变量名列表） |

未修改 `obs/langfuse_mapping.py`；以上事件可直接作为阶段六 trace metadata 的消费点。

## 7. 接入点

- [app.py](file:///d:/Projects/dianshangxiangmu/agent_system/app.py) 模块导入时初始化
  `state_cache = get_session_state_cache()`；
- `_record_route_state(session_id, profile, decision, reason)` 复用 `_route_payload`
  的计算口径，异常全部吞掉；
- `/chat` 与 `/chat/stream` 在 `select_profile` 之后各调用一次，**不改变**响应结构、
  NDJSON 事件与 `route` 字段键集。

## 8. 与后续步骤的衔接

- **步骤 3（pending store 迁 Redis）**：复用同一客户端与前缀约定，建议 key 模板
  `{prefix}:pending:{action_id}`；
- **步骤 4（语义缓存）**：复用 `REDIS_KEY_PREFIX`，建议 key 模板
  `{prefix}:semantic:{stable_prefix_hash}:{normalized_msg_hash}`；
- 本步骤的 `SessionStateCache.get()` 可作为“通用 profile 粘滞”的数据来源，
  但粘滞本身未实现（见遗留）。

## 9. 测试与验证

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_session_state_cache.py` | 7 | 未配置 no-op、CRUD、confidence 缺省、前缀、不可达降级与事件、单例、成功事件开关 |
| `tests/test_session_state_integration.py` | 5 | `/chat` 与 `/chat/stream` 写入、pending 粘滞、缓存故障不影响响应、协议键集不变 |
| `tests/test_memory_integration.py`（扩展） | 原 4 + 新增 5 | TTL=0 不调用 expire、TTL=60 生效、读取不续期、前缀生效、非法前缀回退并记事件 |

`pytest tests/`：**276 passed**（259 既有 + 17 新增）。
既有 259 项断言**零调整**；前缀为空时 `chat:{sid}` 形态保持不变。

端到端验证（临时最小 RESP 服务，验证后已删除；本机 Docker daemon 未运行）：

```text
/chat -> 200, route=order/订单与物流/rule/0.6667
HGETALL session_state:state-s1 ->
  last_route_agent=order, last_route_display_name=订单与物流,
  last_route_strategy=rule, last_route_confidence=0.6667,
  last_route_reason=router, last_route_at/last_activity_at=<epoch>
TTL chat:state-s1 -> -1（未配置 TTL）

REDIS_KEY_PREFIX=tenantA + REDIS_SESSION_TTL_SECONDS=60:
  LRANGE tenantA:chat:p1 -> [{"role": "user", "content": "你好"}]
  TTL tenantA:chat:p1 -> 59
  HGETALL tenantA:session_state:p1 -> 字段完整

REDIS_URL=redis://127.0.0.1:1 + REDIS_FALLBACK_TO_MEMORY=true:
  /chat -> 200，route 结构不变
  /chat/stream -> 200，无 error 事件
  日志出现 session_state_write_failed（session_id_hash=7290fc1f, op=set_route, reason=timeout）
```

## 10. 遗留

- **通用 profile 粘滞未实现**：需要“时间窗口 + 置信度阈值 + 当前 Profile 是否仍允许该工具”
  三条约束一起决策，单独落在步骤 2 会与步骤 3/4 的缓存语义交叉；待会话状态缓存运行稳定后
  再设计；
- 未引入 `fakeredis`，单测使用 mock 客户端；
- 会话状态未接入 Langfuse span，仅有结构化事件作为阶段六接入点；
- `session_state_updated` 默认关闭是日志量取舍；需要时用 `SESSION_STATE_LOG_SUCCESS=true` 打开；
- 会话状态写发生在 `/chat` 与 `/chat/stream` 的请求路径上，Redis 首次不可达时会有一次
  约 1 秒的连接超时（随后由 PING 缓存吸收）；如需严格零阻塞，可在步骤 3 引入后台写入队列。

# 阶段五步骤 3：Pending Store 迁移 Redis

本步骤实现 `RedisPendingStore` + 分布式锁，让两阶段确认记录在多 worker 间共享；
`InMemoryPendingStore` 保留为未配置 Redis 时的默认实现与测试路径。

## 1. 改动前基线（读代码记录）

| 项目 | 实际实现 |
|---|---|
| `PendingActionStore.create` | 协议只写“写入一条新记录并返回它”，**未规定重复 behavior** |
| `InMemoryPendingStore.create` | 同一 `action_id` 再次创建 → `ValueError(f"pending action 已存在: {id}")`（**并非幂等**，与步骤 Prompt 4.3.2 的描述不一致） |
| `get` | 返回内部对象引用；不存在返回 `None` |
| `get_by_session` | 按 `created_at` 升序；不过滤 `expired` |
| `mark` | 先校验 status 合法性 → 不存在抛 `KeyError(f"pending action 不存在: {id}")` → 同状态幂等（可补 result）→ 非法迁移 `ValueError(f"非法状态迁移: {old} -> {new} (action_id={id})")` |
| `purge_expired` | 只处理 `status == "pending"`，返回条数 |
| `clear_session` | 删除会话全部记录，返回条数 |
| `Guardrails.intercept_write` | `purge_expired` → `get_by_session` 去重 → 上限淘汰 → `create` |
| `Guardrails.confirm` | `purge_expired` → 取记录 → 状态检查 → `mark confirmed` → 执行写工具 → `mark executed` |
| `Guardrails.reject` | `purge_expired` → 取记录 → `mark rejected`，返回 `pending.status` |
| 既有测试 | `test_pending_store.py` / `test_guardrails.py` 只使用内存实现，不依赖 Redis；无测试硬编码 `chat:{sid}` 以外的 key |

**两处与 Prompt 假设不一致，已在实现中处理**：

1. `InMemoryPendingStore.create` 重复创建抛错，而 4.3.2 要求 Redis 实现幂等返回。本步骤按 4.3.2 实现 Redis 幂等；
   Guardrails 在 create 前已按“会话 + 工具 + 参数指纹”去重，正常流程不会触发该差异（记为遗留）。
2. `Guardrails.reject` 返回的是 `mark` 前的对象状态；内存实现原地修改所以恰好正确，
   Redis 实现返回新对象会导致返回 `pending`。已将 reject 改为使用 `mark` 的返回值，
   内存行为不变（同一对象），Redis 行为与内存一致。

## 2. Key 全景表

| 用途 | Key 模板 | 结构 | TTL |
|---|---|---|---|
| 历史消息 | `{prefix}:chat:{sid}` | LIST | `REDIS_SESSION_TTL_SECONDS`（默认 0） |
| 会话状态 | `{prefix}:session_state:{sid}` | HASH | `REDIS_SESSION_STATE_TTL_SECONDS`（默认 1800） |
| pending 主记录 | `{prefix}:pending:{action_id}` | HASH | `PENDING_REDIS_TTL_SECONDS`（默认 3600） |
| pending 会话索引 | `{prefix}:pending:session:{sid}` | SET | 无独立 TTL，随记录清理 |
| pending 过期索引 | `{prefix}:pending:expiry` | ZSET（score=expires_at） | 无 TTL |
| pending 锁 | `{prefix}:pending:lock:{action_id}` | STRING（SET NX EX） | `PENDING_LOCK_TTL_SECONDS`（默认 10） |

为什么 pending 不塞进 `session_state`：pending 有独立状态机、独立 TTL 与单会话上限检查，
混入会话状态会让两类数据的生命周期无法独立控制。

## 3. 三组 key 的原子性与维护点

| 操作 | 主记录 | 会话索引 | 过期索引 |
|---|---|---|---|
| `create` | HSET | SADD | ZADD（score=expires_at） |
| `mark(confirmed)` | HSET status | 不变 | 不变 |
| `mark(executed/rejected/expired)` | HSET status/result | SREM | ZREM |
| `purge_expired` | 经 `mark(expired)` 更新 | SREM | ZREM |
| `clear_session` | DEL | DEL | ZREM 每条 |

- `create` 的三组写入用 `pipeline(transaction=True)`：简单多命令，无需 Lua；
- `mark` 用 `WATCH + MULTI/EXEC` 乐观锁：读-改-写窗口极短、只影响单个 `action_id`，
  冲突重试 3 次后抛 `RuntimeError`；
- `confirm` 的整段执行（多步 Redis 操作 + 工具调用）才需要分布式锁。

## 4. 两层过期与硬约束

| 层 | 字段/配置 | 作用 |
|---|---|---|
| 业务层 | `PendingAction.expires_at` | `purge_expired` 据此把 pending 转为 expired；`confirm` 的过期判断依据 |
| Redis 层 | `PENDING_REDIS_TTL_SECONDS` | 主记录自动消失，回收终态记录 |

**硬约束**：`PENDING_REDIS_TTL_SECONDS >= 2 × CONFIRMATION_TTL_SECONDS`，
在 `RedisPendingStore.__init__` 校验，违反时抛中文 `ValueError`。
否则业务层 pending 还没过期，Redis 记录已经消失，`confirm` 会误报
`confirmation_expired`（或更糟：记录丢失导致无法确认）。

`purge_expired` 不直接 DEL：终态记录保留一段时间供审计与幂等返回，由 Redis TTL 自然回收。

## 5. 分布式锁

- `SET key token NX EX ttl` 获取；`token = uuid4().hex`；
- 释放用 Lua：value 匹配才 DEL，避免误删其他实例的锁（`redis-py.Lock` 的边界语义不直观，故手写）；
- 获取失败 `yield False`，`Guardrails.confirm` 映射为 `confirmation_mismatch`；
- 释放时 value 不匹配记 `pending_lock_release_mismatch`；
- 锁 TTL 默认 10 秒：远大于 JSON 写操作耗时（<100ms），又远小于业务 TTL；
- Redis 不可达抛 `RedisUnavailable`，由 confirm 映射为 409 + `confirmation_failed(error_code=redis_unavailable)`。

## 6. `Guardrails` 的最小化扩展

- `__init__` 新增 `lock_factory: Callable[[str], ContextManager[bool]] | None = None`，
  默认 `None` → `nullcontext(True)`，内存后端行为逐字不变；
- `confirm` 仅增加 `with lock_context as acquired:` 包裹与锁失败分支；
- `intercept_write` / `reject` 不加锁（创建天然去重、拒绝是单次幂等写）；
- `get_guardrails()` 注入 `_dynamic_lock_factory`：根据 `pending_backend_name()` 决定
  返回 `nullcontext(True)` 还是 `acquire_pending_lock(action_id)`；
- `intercept_write` 捕获 `RedisUnavailable` → `confirmation_required`
  （`reason=pending_store_unavailable`），绝不冒险直接执行写操作；
- `confirm` 捕获 `RedisUnavailable` → `confirmation_mismatch` + `confirmation_failed`；
- `reject` 改用 `mark` 的返回值，保证两种后端返回同一状态。

**零行为变化证明**：默认 `lock_factory=None` 时 `confirm` 执行的是
`nullcontext(True)`，`with` 块内代码与改造前逐行一致；既有 276 项测试全部通过。

## 7. 后端选择

| `PENDING_STORE_BACKEND` | `REDIS_URL` | 结果 |
|---|---|---|
| `auto`（默认） | 空 | `InMemoryPendingStore`（reason=auto_default） |
| `auto` | 已配置且可达 | `RedisPendingStore`（reason=config） |
| `auto` | 已配置但不可达 | 抛 `RedisUnavailable`，**不回退内存** |
| `memory` | 任意 | `InMemoryPendingStore`（reason=forced） |
| `redis` | 任意 | `RedisPendingStore`；不可达抛 `RedisUnavailable` |

**为什么不自动回退**：pending 是写操作的两阶段状态；Redis 不可达时若各实例悄悄写内存，
同一笔退款可能在两个实例上各执行一次。宁可显式失败让运维介入。

## 8. 隐私与日志字段

| 事件 | 字段 |
|---|---|
| `pending_store_selected` | `backend`、`reason` |
| `pending_lock_acquired`（DEBUG） | `action_id_hash`、`op` |
| `pending_lock_release_mismatch` | `action_id_hash`、`reason` |
| `pending_redis_failed` | `op`、`reason`（显式 `session_id=None`，避免上下文补齐原文） |

`action_id` 使用 `sha256(...)[:8]`。`confirmation_failed` 沿用阶段三既有事件结构
（其中已有 `action_id`/`session_id` 字段），本步骤未改变其 schema。

## 9. 与阶段三步骤 4 遗留的对照

- ✅ 已解决：pending 仅进程内、多 worker 不可见；缺少跨进程 action_id 唯一性（Redis 主记录 + 索引）；
- ❌ 未解决：无后台清理线程（仍依赖 `purge_expired` 调用点触发）；
- ❌ 未解决：写操作无幂等键（阶段三遗留，留给后续）；
- ⚠️ 时钟问题：`PendingAction.created_at/expires_at` 使用 `time.monotonic()`，
  跨主机不可比较；多实例下 `purge_expired` 的时间判断需要统一时钟（见遗留）。

## 10. 与阶段五步骤 4 的衔接

语义缓存不依赖 pending，但复用同一个 `RedisManager` 与 `REDIS_KEY_PREFIX`；
pending 的锁模式（SET NX EX + Lua 释放）可作为语义缓存“防击穿”锁的参考实现。

## 11. 测试与验证

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_redis_pending_store.py` | 10 | 三组 key、幂等 create、状态机与文案、WATCH 重试、过期计数、清会话、前缀、TTL 约束、故障映射 |
| `tests/test_pending_store_backend.py` | 7 | auto/memory/redis 选择、显式失败、单例、内存与 Redis 的 intercept/confirm/reject 行为一致性 |
| `tests/test_guardrails_redis_backend.py` | 6 | 锁竞争 409、Redis 故障 409/428、跨实例可见性、过期确认、TTL 约束 |

`pytest tests/`：**299 passed**（276 既有 + 23 新增）；既有断言零调整。

唯一的既有测试文件改动：`tests/test_session_state_integration.py` 的 autouse fixture
显式设置 `PENDING_STORE_BACKEND=memory`，避免该文件设置 `REDIS_URL` 后被步骤 3 的
RedisPendingStore 选中；断言未改动。

端到端（临时最小 RESP 服务，支持 WATCH/MULTI/EXEC 与 SET NX EX，验证后已删除）：

```text
backend RedisPendingStore
created True 0736d495331e4b76
cross_instance_get pending 为订单 202611
cross_instance_list ['0736d495331e4b76']
reused True
lock_acquired True
locked_confirm_code confirmation_mismatch
cross_session_code confirmation_mismatch
confirmed True 退款已提交
status_after executed
index_after []
idempotent_again True
```

其他验证：

```text
PENDING_REDIS_TTL_SECONDS=100 + CONFIRMATION_TTL_SECONDS=300
-> ValueError: PENDING_REDIS_TTL_SECONDS 必须 ≥ 2 × CONFIRMATION_TTL_SECONDS...

REDIS_URL=redis://127.0.0.1:1 + PENDING_STORE_BACKEND=auto
-> 进程启动阶段即失败（RedisUnavailable，来自 create_store 的显式失败）
```

## 12. 遗留

- `get_pending_store()` 是惰性的：`app.py` 不在本步骤允许修改范围，因此
  “Redis 不可达导致 uvicorn 启动失败”实际由 `create_store()`（会话存储）在导入时触发；
  若会话存储配置了 `REDIS_FALLBACK_TO_MEMORY=true`，pending 的选择会推迟到首个请求。
  如需严格启动期校验 pending，可在后续步骤把 `get_pending_store()` 加入应用装配；
- `InMemoryPendingStore.create` 重复创建抛错，Redis 实现按本步骤要求幂等返回，
  两者在“直接重复 create”上存在差异（Guardrails 正常流程不会触发）；
- `purge_expired` 仍依赖调用方触发，无后台清理线程；
- `time.monotonic()` 时间戳跨主机不可比较，多实例过期判断建议后续改用墙钟或 Redis TIME；
- `PENDING_REDIS_TTL_SECONDS` 默认值缺少生产数据校准；锁 TTL 未按写工具耗时自适应；
- 未引入 `fakeredis`；单测使用 mock 客户端，端到端使用临时 RESP 服务（Docker daemon 未运行）；
- `confirmation_failed` 沿用既有原始 `action_id`/`session_id` 字段，属于全项目既有日志口径，
  本步骤未扩大范围修改。

# 阶段五步骤 3.5：时钟修复与两种 pending 后端协议对齐

本步骤是修复步骤：把 pending 的时间语义从单调钟改为墙钟，加入时钟容差，
统一 `create` 幂等行为，并把 pending 后端选择提前到启动期。

## 1. 缺陷与根因

`time.monotonic()` 的起点是**进程/系统启动时刻**（常见 Linux 上是开机时长），
两个进程各自维护自己的起点，数值不可比较。

复现路径（示意）：

```text
实例 A 的 monotonic = 100s，创建 pending：expires_at = 100 + 3600 = 3700
实例 B 的 monotonic = 5100s（更早启动/不同主机），读取后判断：
    B.now(5100) > A.expires_at(3700)  -> 误判已过期，清理活跃记录
反向场景（A 的起点远大于 B）则永远不过期。
```

影响范围（`grep -rn "monotonic" agent_system/` 的实际结论）：

| 位置 | 结论 | 处理 |
|---|---|---|
| `PendingAction.created_at / expires_at` | 跨实例比较，错误 | 改墙钟 `time.time()` |
| `InMemoryPendingStore.purge_expired` | 跨实例比较，错误 | 改墙钟 + `_is_expired` |
| `RedisPendingStore.purge_expired` | 跨实例比较，错误 | 改墙钟 + `_is_expired` |
| `Guardrails.intercept_write` | 时间戳生成处 | 改墙钟 |
| `Guardrails.confirm / reject / list_pending / _confirmation_payload` | 过期与剩余时间判断 | 改墙钟 + `_is_expired` |
| `Guardrails.confirm` 的 `started` 与执行耗时 | 单请求内计时 | **保留 monotonic**（正确） |
| `LoopController` | 单请求循环超时 | **不改** |
| `RetryExecutor` | 单次重试耗时 | **不改** |
| `RedisManager._ping_cache` | 单进程缓存 TTL | **不改** |
| `ContextAssembler` | 未使用时间戳 | 不涉及 |
| `SessionStateCache` | 已使用墙钟 `time.time()` | 不改（当前不跨实例比较，但语义已是墙钟） |
| `rate_limit.py` / `router.py` / `selection.py` | 单进程窗口/延迟 | 不改 |

**为什么现有测试没发现**：跨实例测试都在同一进程内，两个 mock 实例共享同一个
`monotonic` 起点，掩盖了跨进程不可比较的问题。本步骤新增了真实墙钟注入的跨实例测试。

## 2. 时间戳使用矩阵

| 场景 | 时钟 | 理由 |
|---|---|---|
| pending 创建/过期/判断 | `time.time()` | 跨 worker/跨主机共享，需要 NTP 可比较的墙钟 |
| 单请求超时、重试退避、PING 缓存 | `time.monotonic()` | 只在一个进程内比较，且不能受系统时间回拨影响 |
| 会话状态 `last_route_at` | `time.time()` | 展示/诊断用途，墙钟更直观（步骤 2 已如此实现） |

## 3. 容差设计

`_is_expired(pending, now=None)` 是所有过期判断的唯一入口：

```python
current = time.time() if now is None else now
lifetime = max(0.0, pending.expires_at - pending.created_at)
tolerance = min(CONFIRMATION_CLOCK_SKEW_SECONDS, lifetime)
return current >= pending.expires_at + tolerance
```

- **默认 5 秒**：相对 300 秒业务 TTL 仅 1.7%，对用户体验无感，又能吸收 NTP 跳变；
- **只放宽“已过期”**：`tolerance >= 0`，不会让未过期记录提前失效，方向安全；
- **容差在判断侧**：`expires_at` 是业务承诺，不因实现细节改写；
- **容差不超过 lifetime**：TTL=0（立即过期）不会被容差“续命”；
- **边界取等号**：与旧实现 `expires_at <= now` 一致，避免系统时钟分辨率较粗时
  TTL=0 永远等不到严格大于；
- `purge_expired` 与 `confirm` / `reject` 共用同一函数（测试用 `is` 断言防分叉）。

## 4. `create` 幂等对齐

| 后端 | 改前 | 改后 |
|---|---|---|
| `InMemoryPendingStore.create` | 重复 `action_id` 抛 `ValueError` | 返回既有记录 |
| `RedisPendingStore.create` | 已幂等返回既有记录 | 不变 |

原因：两阶段确认的重试路径可能重复 `create`；后端行为分叉会让同一业务在切换后端时
以不同方式失败，破坏阶段六“切后端一致性”的评测前提。

`mark` / `get` / `get_by_session` / `purge_expired` / `clear_session` 的业务逻辑未改。

## 5. 启动期显式选择 pending 后端

`app.py` 在 `create_store()` 之后调用 `get_pending_store()`：

- `PENDING_STORE_BACKEND=memory`：不触碰 Redis，启动成功；
- `auto`/`redis` 且 Redis 不可达：`RedisUnavailable` 直接上抛，uvicorn 启动失败；
- 成功时记录 `pending_store_bootstrapped`，`backend` 为类名（不记连接串）。

与 `create_store()` 的“显式失败”口径一致：pending 是写操作的两阶段状态，
分叉会导致重复执行，宁可启动失败。

## 6. 与步骤 4 / 阶段六的衔接

- 语义缓存不需要跨实例时间戳比较（TTL 由 Redis 原生 `EXPIRE` 管理），
  本步骤的墙钟语义**不引入**到语义缓存；
- 阶段六“memory ↔ redis 切后端结果一致”的评测前提已建立：
  `create` 幂等、状态机文案、过期判断、清理条数均已对齐。

## 7. 测试

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_pending_clock.py` | 9 | 墙钟生成、容差边界/可配/不超过 lifetime、共用 `_is_expired`、跨实例活跃不误清/真过期被清、monotonic 未被误改 |
| `tests/test_pending_backend_parity.py` | 6 | create 幂等、状态迁移与返回结构、非法迁移/缺失文案、会话隔离、过期清理一致 |

`pytest tests/`：**314 passed**（299 既有 + 15 新增）。

**既有测试调整清单（4 处 setup，非断言；外加 0 条断言）**：

| 文件 | 改动 |
|---|---|
| `tests/test_pending_store.py` | `_pending` 的 `time.monotonic()` → `time.time()`；purge 的 `now` 改为 `time.time()+6` 以越过默认容差 |
| `tests/test_redis_pending_store.py` | `_pending` 与排序/过期用例的基准时间改为 `time.time()` |
| `tests/test_guardrails_redis_backend.py` | `_pending` 的基准时间改为 `time.time()` |
| `tests/test_guardrails.py` / `tests/test_select_profile.py` | 注入“已过期”时改用 `time.time() - 1` |

步骤 Prompt 预计的“`test_pending_store.py` 重复 create 抛 ValueError”断言实际不存在，
因此没有任何断言被删除或弱化。

## 8. 启动验证

```text
默认（无 REDIS_URL）：
  pending_store_bootstrapped backend=InMemoryPendingStore
  import agent_system.app -> OK

REDIS_URL=redis://127.0.0.1:1 + REDIS_FALLBACK_TO_MEMORY=true + auto：
  redis_fallback(sqlite) 后，pending 引导抛 RedisUnavailable: timeout
  进程启动失败（不再是首个请求才失败）

同上 + PENDING_STORE_BACKEND=memory：
  pending_store_bootstrapped backend=InMemoryPendingStore
  启动成功（显式内存后端不触碰 Redis）

grep time.monotonic loop_controller.py retry_executor.py cache/redis_manager.py
  -> 仍有匹配（未被误改）
```

## 9. 遗留

- `SessionStateCache` 的时间戳已是 `time.time()`，本步骤未改；若未来做跨实例
  “最近活跃会话”查询，需要另外确认 NTP 与容差策略；
- `PENDING_REDIS_TTL_SECONDS` 的默认值仍待生产数据校准；
- `PENDING_STORE_BACKEND=auto` + Redis 不可达时，启动失败同时可能来自会话存储
  （`create_store`）或 pending 引导，日志需结合 `store_selected` 与
  `pending_store_bootstrapped` 判断具体阶段；
- Redis 后端的终态记录从会话索引移除，而内存后端 `get_by_session` 仍返回终态记录；
  Guardrails 只消费 pending，因此不影响业务，但严格协议层面仍有这一处差异；
- 未引入 `fakeredis`；跨实例与 Redis 行为使用共享 mock 客户端验证。

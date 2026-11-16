# 阶段三 · 步骤 4 实现说明：Guardrails 与写操作两阶段确认

> 把 `apply_refund` / `cancel_order` 改为**服务端两阶段确认**：首次调用只创建 pending 记录并返回
> `needs_confirmation`，用户明确确认后由新增的 `confirm_action` 触发真正执行；拒绝或超时作废。
> 原有 6 个工具 schema 逐字未改，新增 2 个内部工具（6 → 8）。

## 1. 状态机与决策

### 1.1 状态迁移

```text
pending ──confirm──> confirmed ──执行成功──> executed
   │                     │
   │                     └──执行失败──> rejected
   ├──reject──> rejected
   └──TTL 到期 / 超出单会话上限──> expired
```

| 当前状态 | 允许的下一状态 | 非法迁移示例 |
| --- | --- | --- |
| `pending` | `confirmed` / `rejected` / `expired` | — |
| `confirmed` | `executed` / `rejected` | `confirmed → pending` |
| `executed` | `executed`（同状态幂等） | `executed → confirmed` |
| `rejected` | `rejected` | `rejected → executed` |
| `expired` | `expired` | `expired → executed` |

`InMemoryPendingStore.mark()` 对同状态重复调用幂等；非法迁移抛中文 `ValueError`。

### 1.2 为什么在服务端做状态机

- 提示词约束可被模型忽略、被 prompt 注入绕过，且随模型版本漂移；
- 服务端状态机是唯一执行入口：未确认前 `apply_refund` / `cancel_order` 根本不触碰 datasource；
- 状态与 `action_id` 可审计，后续可接评估（阶段六）与 Langfuse metadata。

## 2. 过期与清理

- 默认 TTL `CONFIRMATION_TTL_SECONDS=300`；
- **不使用后台线程/定时器**：过期判断幂等，`intercept_write` / `confirm` / `reject` / `list_pending`
  前各触发一次 `purge_expired`，避免测试依赖真实时间与多 worker 重复执行；
- `purge_expired` 只把 `pending` 改为 `expired`，已执行记录不会被覆盖；
- 单会话 pending 上限 `CONFIRMATION_MAX_PENDING_PER_SESSION=5`，超出时最旧的 pending 直接作废并记
  `confirmation_expired`。

## 3. 幂等性

| 场景 | 行为 |
| --- | --- |
| 同会话 + 同工具 + 同参数重复拦截 | 复用已有 pending，返回同一 `action_id`（参数指纹 `sha256(args)[:16]`） |
| 重复 `confirm` 且已 `executed` | 直接返回缓存 `result`，不重复执行 |
| `confirm` 时状态为 `confirmed` | 视为并发执行中，抛 409 `confirmation_mismatch` |
| 重复 `reject` | 返回当前状态，不重复记日志 |
| 执行失败后再次 confirm | 记录已是 `rejected`，返回 410 `confirmation_expired`（禁止重试同一 pending） |

## 4. 执行边界与绕过方案（方案 A）

`tools.py` 拆分为：

- `_apply_refund_impl` / `_cancel_order_impl`：真实业务实现（调用 datasource），不加 `_tool_guard`；
- `apply_refund` / `cancel_order`：对外工具，带 `_tool_guard`，负责读取会话、拦截或（应急开关下）直接执行；
- `Guardrails._execute_write` **直接调用 `_impl`**，绕过对外工具，因此不会二次拦截；
- 执行异常通过 `tools.map_tool_exception()` 统一映射（与 `_tool_guard` 同口径）。

选择 A 的理由：显式、无隐式线程状态、不会死循环；B（threading.local 跳过标记）和 C（直连 datasource）
都会把“确认执行”与工具层埋点/异常映射割裂。

## 5. session_id 传递（方案 A：contextvars）

- 复用 `obs/request_id.py` 中与 `request_id` 并列的 `_session_id_var`；
- HTTP 路径由 `RequestContextMiddleware` 自动从请求体解析 `session_id`，端点无需改动；
- 新增公开的 `set_session_id()` / `reset_session_id()`，供 `demo.py` 等无 HTTP 中间件入口显式设置；
- 无会话上下文时**拒绝写操作**：抛 `confirmation_required`（409），绝不静默执行；
- 选择 A 的理由：与现有 request_id 风格一致、无需改 Registry 签名；缺点是非 HTTP 入口需显式设置
  （`demo.py` 已处理）。

## 6. schema 6 → 8

### 6.1 变更内容

- 原有 6 条 schema（`query_product` / `query_order` / `query_logistics` / `search_faq` /
  `apply_refund` / `cancel_order`）**逐字未改**；
- 追加：
  - `confirm_action`：`{"action_id": string}`，描述“确认之前被服务端拦截的写操作并执行。仅在用户明确确认后调用。”；
  - `reject_action`：`{"action_id": string}`，描述“拒绝之前被服务端拦截的写操作并作废。用户反悔或拒绝时调用。”；
- `builtin_tools` 解析实现时先查 `tools`，再查 `confirmation_tools`；两个新工具 tags 为
  `("builtin", "internal")`。

### 6.2 快照对照（有意变更）

| 快照 | 旧值 | 新值 |
| --- | --- | --- |
| 6 条 schema | `4c46bb1e…f9344e` | 仍为 `4c46bb1e…f9344e`（测试 `schemas[:6]` 断言） |
| 8 条 schema | — | `c192bb5d…8fb357` |

`tests/test_builtin_tools_migration.py` 的断言修改（均为有意变更）：

1. `EXPECTED_NAMES` 追加 `confirm_action` / `reject_action`；
2. `EXPECTED_SCHEMA_SHA256` 更新为 8 条快照；
3. 新增 `LEGACY_SCHEMA_SHA256` 断言前 6 条未变；
4. 数量断言 6 → 8（`test_llm_client_tools_matches_registry`、`test_tool_registry` 相关两处）。

## 7. SYSTEM_PROMPT 追加

在原文末尾追加（未删除任何原句）：

```text
写操作确认规则：
- 当 apply_refund 或 cancel_order 工具返回 needs_confirmation=true 时，
  不要断言操作已完成；请向用户展示 summary 中的内容，并请求用户明确确认。
- 用户明确确认后，调用 confirm_action(action_id=<上一步返回的 action_id>)。
- 用户拒绝或改变主意时，调用 reject_action(action_id=...)。
- 未收到明确确认前，不得再次调用 apply_refund 或 cancel_order 以试图绕过确认。
```

哈希变化（有意变更，非回归）：

| 项 | 旧值 | 新值 |
| --- | --- | --- |
| `SYSTEM_PROMPT` sha256 | `9c1e0c0a…bc4a85` | `647f94fa…2d755` |

`tests/test_context_assembler.py` 的 `EXPECTED_SYSTEM_PROMPT_SHA256` 已同步更新；
稳定前缀 hash 因 system 内容变化而重建，属预期。

## 8. Mock 路径策略

- `MockLLM.reply` 当前**没有**退款/取消订单的规则分支，因此不会触发写操作；
- 本步骤**未**在 Mock 中模拟两阶段确认（需维护 pending 状态并识别“确认”语句，超出本步骤范围）；
- Mock 路径行为与之前完全一致；端到端演示如需确认流程，需配置真实 LLM 或按 notes 遗留项后续补充。

## 9. 事件

| 事件 | 字段 |
| --- | --- |
| `confirmation_requested` | `action_id` / `tool` / `session_id` / `ttl_seconds` / `expires_in` |
| `confirmation_granted` | `action_id` / `tool` / `session_id` / `execute_latency_ms` |
| `confirmation_rejected` | `action_id` / `tool` / `session_id` |
| `confirmation_expired` | `action_id` / `tool` / `session_id` / `age_ms` |
| `confirmation_failed` | `action_id` / `tool` / `session_id` / `error_code` |

- 均由 `log_event` 输出，不新建 span；`langfuse_mapping.py` 未修改；
- 作为 Langfuse 根 span metadata 的接入点留给阶段三后续 / 阶段六；
- 不向 NDJSON 追加任何事件。

## 10. 错误码

| 错误码 | HTTP | retryable | 使用场景 |
| --- | --- | --- | --- |
| `confirmation_required` | 409 | False | 缺少会话上下文、确认工具缺少 action_id 的兜底 |
| `confirmation_expired` | 410 | False | pending 不存在/已过期/已拒绝后再次确认 |
| `confirmation_mismatch` | 409 | False | 跨会话确认、并发执行中（confirmed） |

未新增 `AgentError` 子类，直接构造基础异常；统一处理器/兜底钩子无需改动。

## 11. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `register_all(); len(get_registry().schemas())` | `8` |
| fake LLM 两阶段退款 | `/chat` 200，回复“退款已提交…”，`confirmation_requested/granted` 各一条 |
| 真实数据两阶段 | 以 `20261104001` 实测：拦截后仍为 `已发货`；确认后变为 `退款中`；验证后已还原文件 |
| 跨会话 confirm | 409 `confirmation_mismatch` |
| TTL=0 后 confirm | 410 `confirmation_expired` |
| 无 session 直接 `tools.apply_refund` | 409 `confirmation_required`，不写数据 |
| `CONFIRMATION_ENABLED=false` | 直接执行并返回正常结果 |
| `/chat/stream` | NDJSON 仍为 `start → delta → {"done":true} → 扩展 done`，无新增事件 |
| 事件 | `confirmation_requested` / `granted` 在完整流程各 1 条 |

## 12. 测试

```text
pytest tests/ -q
135 passed in 1.07s
```

新增 24 项：

- `tests/test_pending_store.py`（6）：CRUD、状态迁移/幂等、过期清理、按会话清空、并发创建、单例重置；
- `tests/test_guardrails.py`（12）：拦截不执行、参数幂等/区分、确认与缓存、跨会话、并发 confirmed、
  过期、执行失败转 rejected、拒绝幂等、总开关、单 pending 上限、摘要不含参数原文；
- `tests/test_write_ops_confirmation.py`（6）：fake LLM 两阶段、跨会话、过期、无会话拒绝、
  总开关关闭直执行、NDJSON 结构不变。

既有 111 项回归通过；更新的快照断言见 6.2 / 7 节，均属有意变更。

## 13. 阶段五 Redis 迁移接口

`PendingActionStore` 是 Protocol，`Guardrails` 只依赖这 6 个方法；
阶段五实现 `RedisPendingStore` 并替换 `get_pending_store()` 即可，`Guardrails` / `tools` / 测试无需改动。
需要补的语义：分布式锁（避免并发 confirm 双写）、TTL 过期事件、跨进程 action_id 唯一性。

## 14. 遗留问题

1. **pending 仅进程内**：多 worker / 多实例互相不可见，确认请求必须落到同一进程；阶段五 Redis 解决。
2. **无后台清理线程**：过期记录在下次调用时才清理；长期无请求会滞留内存（数量受单会话上限约束）。
3. **Mock 不模拟确认流程**：Mock 无写操作规则，端到端演示需真实 LLM。
4. **流中错误不清理 pending**：流开始后异常只会终止流，pending 依赖 TTL 过期。
5. **`confirmation_expired` 事件在未知 action 时 tool 记为 `unknown`**：信息有限但避免泄露。
6. **摘要未包含金额**：`apply_refund` 摘要只有订单号与原因；金额需查订单后补充（避免额外数据源调用）。
7. **参数指纹含完整参数哈希**：不泄露值，但相同订单不同原因会被视为不同 pending（符合预期）。
8. **确认执行绕过 `_tool_guard` 的 tool_call 事件**：执行通过 `_impl` 完成，只有 `confirmation_granted`
   记录耗时；`apply_refund` 的拦截调用本身仍有 `tool_call` 事件。

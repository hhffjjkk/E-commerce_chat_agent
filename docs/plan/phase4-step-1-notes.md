# 阶段四 · 步骤 1 实现说明：AgentProfile 与子 Agent 工具子集

> 新增子 Agent 静态声明：售前 / 订单与物流 / 售后，明确各自专属 prompt、工具子集与写权限。
> 本步骤**不接入路由**，`/chat`、`/chat/stream`、`SYSTEM_PROMPT`、8 条工具 schema 均未改动。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增 | `harness/agent_profile.py`、`harness/builtin_profiles.py` |
| 修改 | `harness/builtin_tools.py`（仅 tags 补 `write`）、`harness/__init__.py` |
| 内置 Profile | `pre_sales` / `order` / `after_sales` |
| 工具 schema | 仍为 8 条，未改描述/参数/顺序 |
| 行为影响 | 零：未接入 Router，未改 agent/app/stream/runtime/errors |
| 测试 | 165 passed → **181 passed**（新增 16 项） |

## 2. 为什么按业务域划分

选择“售前 / 订单 / 售后”而不是按工具数量、模型大小或复杂度划分，原因：

1. **用户意图天然按业务域聚类**：商品咨询、订单物流、退款售后是客服场景最稳定的三类诉求，
   与现有工具的业务语义一一对应；
2. **权限边界清晰**：售前与订单只读，售后才持有写权限，便于把“最小权限”落实到 Profile；
3. **prompt 可维护**：每个子 Agent 的专属规则短小、可独立演进，避免一个巨型 prompt 里互相冲突；
4. **可评估**：阶段六可按 `profile.name` 分组统计成功率、工具命中与成本。

不按工具数量划分的原因：工具增减会让 Profile 边界漂移；不按模型划分的原因：模型选择是运行策略，
不是业务边界。

## 3. 工具子集与写权限

| Profile | display_name | 路由描述 | tool_names | allowed_write_tools |
| --- | --- | --- | --- | --- |
| `pre_sales` | 售前咨询 | 商品信息、价格、库存、功能咨询、选购建议、常见问题 | `query_product`, `search_faq` | `()` |
| `order` | 订单与物流 | 订单状态、物流轨迹、收货信息查询 | `query_order`, `query_logistics` | `()` |
| `after_sales` | 售后服务 | 退款、取消订单、退货、售后处理 | `query_order`, `apply_refund`, `cancel_order`, `confirm_action`, `reject_action` | `apply_refund`, `cancel_order` |

### 3.1 为什么售后需要 `query_order`

`_apply_refund_impl` 会校验订单状态；但用户常先问“我这单能退吗”。先查订单既能给用户可解释的
前置判断，也能避免模型在不知道状态时直接触发写操作。

### 3.2 为什么 `confirm_action` / `reject_action` 不算写权限

它们本身不产生新的业务副作用，只是幂等推进/作废 pending 状态机；因此放在 `tool_names` 中让售后
Agent 能调用，但不放入 `allowed_write_tools`。真正会修改 `orders.json` 的仍是
`apply_refund` / `cancel_order`。

### 3.3 `allowed_write_tools` 与 Registry `write` 标签

- `tool_names` 表达“这个 Agent 能拿到哪些工具 schema”；
- `allowed_write_tools` 表达“这个 Agent 被允许触发哪些有副作用的工具”，是路由层的意图声明；
- Registry 的 `"write"` 标签是校验依据：`allowed_write_tools` 中的每个工具必须同时满足
  “在 tool_names 中”且“Registry 带 write 标签”，否则 `validate()` 抛错；
- 两层仍然不能替代 Guardrails：即使 Profile 声明了写权限，实际执行依旧要经过两阶段确认。
  这是纵深防御，而不是重复设计。

## 4. 共享规则与专属规则

### 4.1 拆分

- **共享规则**：直接读取 `agent.SYSTEM_PROMPT`（通过 `BASE_RULES_PROVIDER` 延迟导入），
  包含角色、转人工、写操作确认等全局底线；
- **专属规则**：各 Profile 的 `system_prompt`，只描述该业务域的额外边界与工具使用方式；
- 未复制 `SYSTEM_PROMPT`，避免两份规则漂移；也未修改其原文。

### 4.2 组合顺序

```text
BASE_RULES_PROVIDER()
\n\n## 当前子 Agent：<display_name>\n\n
profile.system_prompt
```

从稳定到易变：

1. 共享规则：所有 Profile 相同；
2. 标题：Profile 固定即逐字稳定；
3. 专属规则：设计期常量。

因此同一 Profile 的 `compose_system_prompt` 输出对 ContextAssembler 的
`stable_prefix_hash` 是稳定的，不会随请求抖动。这也是禁止把动态内容拼入 prompt 的原因。

## 5. 注册顺序与幂等

```python
register_all()                 # 先注册 8 个工具（validate 依赖 Registry 与 write 标签）
register_builtin_profiles()    # 再注册 3 个 Profile
```

- `ProfileRegistry` 单例**不自动注册**内置 Profile，避免循环导入与测试污染；
- `register_builtin_profiles()` 幂等：同名且 spec 完全相同则跳过，不同则抛
  `ValueError("builtin Profile 定义被并发修改")`；
- 若 Tool Registry 为空，`validate()` 抛出带 `register_all` 提示的 `ValueError`；
- 注册在 `app.py` 中尚未调用（本步骤不接入路由），阶段四步骤 2/3 接入时再补。

## 6. `builtin_tools.py` 的 tags 变更

仅追加标签，不改 schema / callable / 数量：

| 工具 | 变更前 | 变更后 |
| --- | --- | --- |
| `apply_refund` | `("builtin", "after_sales")` | `("builtin", "after_sales", "write")` |
| `cancel_order` | `("builtin", "after_sales")` | `("builtin", "after_sales", "write")` |

其余 6 条未变。`schemas()` 只序列化 name/description/parameters，不含 tags，
因此 `test_builtin_tools_migration.py` 的 schema 快照哈希不变，既有断言无需修改。

## 7. 不接入路由

本步骤**没有**修改：

- `/chat`、`/chat/stream`、`/webhook`；
- `SYSTEM_PROMPT`、`_build_messages`、`reply` / `reply_stream`；
- NDJSON 事件、错误结构、工具 schema。

`/chat` 与 `/chat/stream` 行为与阶段三完成时完全一致；Profile 目前只是可测试的数据结构。

## 8. 阶段四后续衔接点

| 后续步骤 | 消费方式 |
| --- | --- |
| 步骤 2 Router | 读取 `ProfileRegistry.list()` 与每个 `description`，生成路由候选；`profile.name` 作为路由结果 |
| 步骤 3 接入 | `_build_messages(..., profile)` 使用 `compose_system_prompt(profile)`；`registry.schemas(names=profile.tool_names)` 下发子集 |
| NDJSON `route` 事件 | `agent` 字段填 `profile.name`，`confidence` / `reason` 来自 Router |
| 步骤 4 路由透出 | 前端展示当前子 Agent；失败时回退到默认 Profile |
| 阶段六评测 | 按 `profile.name` 与 `policy_id` 联合分组 |

## 9. 验收与实测

| 检查 | 结果 |
| --- | --- |
| 注册后名称顺序 | `['pre_sales', 'order', 'after_sales']` |
| 工具子集/写权限 | 与第 3 节表格逐项一致（测试断言） |
| 所有工具已注册 | 3 个 Profile 的 `tool_names` 均在 Registry |
| `compose_system_prompt` | 以 `SYSTEM_PROMPT` 原文开头，含 `## 当前子 Agent：售前咨询` 与专属关键句 |
| 稳定性 | 同 Profile 两次调用逐字相等；3 个 Profile 的 sha256 互不相同 |
| base 变化 | monkeypatch `BASE_RULES_PROVIDER` 后 hash 变化 |
| 幂等 | 重复 `register_builtin_profiles()` 不抛异常、快照一致 |
| 空 Registry | 抛 `ValueError` 且提示 `register_all` |
| 既有行为 | `/chat`、`/chat/stream`、schema 数量 8、错误结构均未变 |

## 10. 测试

```text
pytest tests/ -q
181 passed in 1.32s
```

新增 16 项：

- `tests/test_agent_profile.py`（8）：合法校验、名称、未注册工具、写权限子集、write 标签、
  空 prompt、Registry CRUD/覆盖/快照、单例重置；
- `tests/test_builtin_profiles.py`（8）：注册顺序、工具子集逐项、Registry 一致性、write 标签、
  prompt 前缀/标题/关键句、hash 稳定与互异/base 变化、注册幂等、空 Registry 报错。

既有 165 项全部通过，未修改任何既有断言。

## 11. 遗留问题

1. **Profile 未接入运行时**：Router 与 `_build_messages` 的消费分别在步骤 2、3。
2. **`model_override` 未消费**：字段已预留，实际模型切换未实现。
3. **没有默认兜底 Profile**：Router 失败时的回退策略在步骤 2 定义。
4. **`allowed_write_tools` 尚不参与运行时校验**：当前 Guardrails 对所有写操作统一拦截；
   后续可在 Router/执行层校验“当前 Profile 是否允许该写工具”。
5. **Profile tags 较粗**：目前只有 business + 域标签，管理端分类可按需扩展。
6. **未做 prompt 快照锁定**：测试只断言关键句与 hash 稳定性，避免文案微调导致脆弱；
   阶段六 prompt 版本化时再引入快照。
7. **Profile 注册未在 app 启动时调用**：接入路由时需补 `register_all()` +
   `register_builtin_profiles()` 的启动顺序。

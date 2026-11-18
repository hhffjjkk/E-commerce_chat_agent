# 评测任务集说明

本目录的 `tasks.jsonl` 是固定离线评测任务集：每行一个 JSON 对象，按 `category` 分组排列。
评测器位于 `agent_system/eval/`，运行方式：

```bash
python -m agent_system.eval.runner --tasks evals/tasks.jsonl --mode mock
```

报告写入 `reports/eval-<时间>-<模式>.json` 与 `.md`。

## 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 稳定标识，任务集内唯一；同时作为 Langfuse dataset item id。 |
| `category` | 是 | 分类，用于分组统计；取值见下表。 |
| `session_id` | 否 | 会话 ID，默认 `eval-<id>`；多轮任务共享同一值。 |
| `messages` | 是 | 用户消息数组，至少 1 条；多轮场景按顺序发送。 |
| `expect` | 否 | 期望规格，字段全部可选。 |
| `only_real_llm` | 否 | `true` 表示 Mock 模式跳过（依赖真实 LLM 能力）。 |
| `notes` | 否 | 测试意图；会出现在报告的任务清单与失败清单里。 |
| `profile_hint` | 否 | 人读提示；实际判定以 `expect.route_agent` 为准。 |
| `inject` | 否 | 评测层故障注入，仅用于 `error_path` 与 `pending_flow`。 |

### `expect` 子字段

| 字段 | 判定方式 | 不适用条件 |
|---|---|---|
| `route_agent` | 最后一次请求的 `route_selected.agent` 与之相等 | 未填写（null） |
| `contains` | 最后一次回复包含全部关键词 | 空数组 |
| `no_write_tools` | 本次任务未调用任何带 `write` 标签的工具 | 显式设为 `false` |
| `cache_hit` | 任务内是否出现 `cache_hit` 事件与期望一致 | 未填写（null） |
| `errors_allowed` | 实际错误码集合是它的子集（默认空 = 不允许错误） | 不适用，始终判定 |

任务通过当且仅当所有适用的 evaluator 都通过。

### `inject` 子字段

| `type` | 附加字段 | 作用 |
|---|---|---|
| `tool_error` | `tool` | 让指定数据源方法抛异常，验证工具异常映射。 |
| `llm_error` | 无 | 替换 bot 为抛 `LLMError` 的桩，验证 LLM 异常路径。 |
| `route_exception` | 无 | 让 Router 抛异常，验证默认 Profile 回退。 |
| `confirmation_ttl` | `value` | 临时修改 Guardrails TTL，验证 pending 过期。 |

注入只修改评测进程内的引用，运行结束立即恢复，不会改业务代码或仓库数据。

## 分类约定

| category | 含义 | 当前条数 |
|---|---|---|
| `route_rule_hit` | 规则路由明确命中售前/订单/售后 | 5 |
| `route_llm_fallback` | 规则低置信，需要 LLM 兜底 | 2 |
| `pending_flow` | 两阶段确认：确认 / 拒绝 / 过期 | 3 |
| `cache_hit` | 同一消息重复发送，第二次命中缓存 | 3 |
| `cache_miss_not_cached` | after_sales / 短消息 / 纯数字，不缓存 | 4 |
| `error_path` | 注入工具、LLM 或路由异常 | 3 |
| `boundary_input` | 空消息 / 超长消息 / 不存在的订单 / 纯标点 | 4 |
| `tool_subset` | 通过响应行为间接验证 Profile 工具子集 | 3 |

## 如何新增一条任务

1. 先确定 `category` 与 `id`；`id` 一经发布不要改名（Langfuse dataset 用它做幂等键）。
2. 只依赖任务自身能自然产生的状态：需要 pending 时必须用前一条消息创建，不能预置 Redis 数据。
3. `expect` 只写能稳定判定的内容：
   - 路由类任务写 `route_agent`；
   - 查询类任务写回复里稳定出现的价格/状态关键词；
   - 缓存类任务写 `cache_hit`；
   - 错误类任务用 `errors_allowed` 列出唯一预期的错误码。
4. `session_id` 统一用 `eval-<id>`，避免与手工联调或其它测试冲突。
5. 在 `notes` 里写清“为什么这样设期望”，报告会直接引用。
6. 运行 `pytest tests/eval/test_task_schema.py` 确认格式合法。

## `only_real_llm` 的适用场景

- 依赖 LLM 语义路由的模糊表述（`route_llm_fallback`）；
- 依赖 LLM 主动发起写工具调用的 pending 流程（`pending_flow`）。

上述场景在 Mock 模式下无法自然触发（`MockLLM` 是关键词规则引擎，不调用写工具），
因此标记为 `only_real_llm`。Mock 运行会把它们计入“跳过”，不计入通过率分母。

## 与 pytest 的关系

- `tests/eval/` 是单元/集成测试：验证数据类、采集器、统计口径、报告生成与 runner 流程；
- 本目录的 `tasks.jsonl` 是“评测数据集”：用于产出可比较的指标报告。
- 前者保证评测器正确，后者回答“当前 Agent 表现如何”。

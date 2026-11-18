# 阶段六步骤 1：固定任务集评测与指标口径 — 实现说明

## 1. 改造前基线（读代码确认）

| 对象 | 基线行为 |
|---|---|
| `agent_system/eval/runner.py` | 读取 `evals/tasks.jsonl`，通过 httpx `ASGITransport` POST `/chat`，按 `X-Request-ID` 从日志 handler 过滤事件；只输出内存汇总 JSON，无报告文件。 |
| `evals/tasks.jsonl` | 3 条，字段为 `{"input", "expected_tool"}`。 |
| `code_evaluators.py` | `tool_success_rate(events)` / `error_rate(events)` / `evaluate_trajectory(events)` / `report_scores(client, trace_id, scores)`。 |
| `llm_call`（phase=end） | 字段为 `model / stream / ok / latency_ms / error_type`，**没有** `prompt_tokens` / `completion_tokens` → 本步骤已补齐。 |
| `route_selected` | `agent / display_name / strategy / reason / confidence / latency_ms`。 |
| `tool_call` | `tool / args_keys / ok / latency_ms[/error_type]`。 |
| `cache_*` | `cache_hit / cache_miss / cache_write / cache_write_skipped`，均在 `app.py` 产生；缓存默认关闭。 |
| `confirmation_*` | `confirmation_requested / granted / rejected / expired / failed`，在 `guardrails.py` 产生。 |
| `MockLLM.reply` | 返回字符串；只按关键词调用 `query_logistics / query_order / query_product`，**不会调用写工具**。 |
| `tools._source` | `JsonDataSource`，`apply_refund / cancel_order` 会写 `data/orders.json`。 |

## 2. 执行入口与隔离策略

**入口**：runner 通过 ASGI `/chat` 执行，而不是直接调 `bot.reply`。原因：`route_selected` 与 `cache_*` 事件都在 app 层产生，直接调 bot 会丢失路由/缓存指标；用 `/chat` 不需要复制 app 的路由与缓存编排逻辑，同时 `bot.reply` 仍被真实执行。

**隔离**（`_EvalEnvironment`）：
- 会话库：每次运行使用临时 SQLite，结束关闭连接并删除；
- pending：强制内存后端，运行结束恢复原环境；
- 数据源：`data/*.json` 的内存副本，写操作不落盘，任务集可重复运行；
- Mock 模式：安装进程内 FakeRedis，并临时把 `app_module.settings.nmsg_cache_enabled` 置 True；每个任务前清空 FakeRedis 的字典（**不使用 FLUSHDB**）；
- 运行结束恢复 bot / settings / cache / store / 数据源 / 环境变量，并重置相关单例。

**事件采集**：`EvalCollector.install()` 同时替换 `obs.events.log_event` 与所有已加载 `agent_system.*` 模块中直接绑定的 `log_event`（项目普遍使用 `from ... import log_event`，只 patch events 模块不生效）。串行执行 + `start_task/stop_task` 边界保证事件归属，不依赖 request_id 过滤。

## 3. 任务集（27 条，8 分类，5 条 only_real_llm）

| task_id | category | 测试意图 |
|---|---|---|
| rule-presales-product | route_rule_hit | 规则命中「多少钱」，售前查询商品价格。 |
| rule-presales-laptop | route_rule_hit | 商品关键词命中售前，验证价格回复。 |
| rule-order-status | route_rule_hit | 规则命中「订单」，返回订单状态。 |
| rule-order-logistics | route_rule_hit | 规则命中「单号/到哪了」，验证物流链路。 |
| rule-after-sales-intent | route_rule_hit | 规则命中「退款」，售后 Profile 接住写操作意图。 |
| llm-route-vague-product | route_llm_fallback | 模糊咨询依赖 LLM 路由为售前（仅真实模式）。 |
| llm-route-vague-after-sales | route_llm_fallback | 模糊售后诉求依赖 LLM 路由（仅真实模式）。 |
| pending-confirm | pending_flow | 创建 pending → 用户确认执行（仅真实模式）。 |
| pending-reject | pending_flow | 创建 pending → 用户拒绝（仅真实模式）。 |
| pending-expire | pending_flow | TTL 置 0，确认时触发 confirmation_expired（仅真实模式）。 |
| cache-presales-repeat | cache_hit | 同一售前消息两次，第二次命中缓存。 |
| cache-order-repeat | cache_hit | 订单消息重复发送，第二次命中缓存。 |
| cache-presales-lamp | cache_hit | 另一商品验证缓存 key 与商品类别无关。 |
| cache-miss-after-sales | cache_miss_not_cached | after_sales 不在白名单，重复发送也不命中。 |
| cache-miss-short-message | cache_miss_not_cached | 短消息低于最小长度，不查不写。 |
| cache-miss-pure-number | cache_miss_not_cached | 纯数字消息不缓存。 |
| cache-miss-cancel-intent | cache_miss_not_cached | 售后取消意图同样不缓存。 |
| error-tool-exception | error_path | 注入数据源异常，映射为 tool_failed。 |
| error-llm-timeout | error_path | 注入 LLM 超时，映射为 llm_failed。 |
| error-route-fallback | error_path | 路由抛异常时回退默认 Profile 且不中断。 |
| boundary-empty-message | boundary_input | 空消息走兜底回复。 |
| boundary-long-message | boundary_input | 超长消息仍能查到商品实体。 |
| boundary-unknown-order | boundary_input | 不存在的订单号返回业务失败而非异常。 |
| boundary-punctuation-only | boundary_input | 纯标点归一化为空，不缓存、不报错。 |
| subset-presales-product | tool_subset | 售前商品查询行为间接验证工具子集。 |
| subset-order-logistics | tool_subset | 订单 Profile 的物流工具。 |
| subset-order-status | tool_subset | 订单 Profile 的订单状态工具。 |

`only_real_llm` 适用场景：依赖 LLM 语义路由的模糊表述，以及依赖 LLM 主动发起写工具的 pending 流程。Mock 模式跳过并计入报告 `skipped`，不计入通过率分母。

## 4. 指标口径

| 指标 | 定义 | 测量边界 |
|---|---|---|
| 通过率 | 通过任务数 / 实际执行任务数 | 跳过任务不计入分母；所有适用 evaluator 都通过才算通过。 |
| 单任务 LLM 调用次数 | `llm_call(phase=start)` 计数 | 只计业务 LLM 调用；路由 LLM 走 `route_llm_call`，不计入。 |
| Token 用量 | `llm_call(phase=end)` 的 `prompt_tokens` / `completion_tokens` 累加 | 失败调用按 0 计；Mock 模式恒为 0（不调用真实模型）。 |
| 成本 | `prompt/1000*输入单价 + completion/1000*输出单价` | 单价默认 0 → 报告显示“未配置单价”，不显示 0 成本。 |
| 延迟 | 任务级墙钟耗时（含 ASGI 调度与本地 IO） | Mock 不含网络；真实模式包含 LLM/工具网络耗时。 |
| P50/P95 | nearest-rank，任务级延迟排序后取第 `ceil(p*n)` 个 | 不插值；按任务级而非 LLM 调用级：用户感知的是任务完成时间。 |
| 缓存命中率 | `cache_hit / (cache_hit + cache_miss)` | 只统计 nmsg 缓存事件；无请求时给 0.0。 |
| 分组统计 | 按 category / route_agent 聚合总数、通过数、P95、平均 LLM 调用 | Profile 取最后一次 `route_selected.agent`。 |

**成本单价来源**：`EVAL_INPUT_PRICE_PER_1K_TOKENS` / `EVAL_OUTPUT_PRICE_PER_1K_TOKENS`。不硬编码任何供应商价格，因为定价会随供应商与时间变化。

**缓存节省估算**：`cache_hit_count × 平均每任务 LLM 调用数`。局限：假设每次命中等价于一次完整任务的全部 LLM 调用；同一任务重复命中会高估，只能用于趋势对比，不作为账单依据。

## 5. Mock 与真实 LLM 的差异

- Mock 模式：`MockLLM` 是关键词规则引擎，不调用 LLM，因此 LLM 调用数与 Token 恒为 0；不发起写工具，pending 流程无法触发；缓存使用进程内 FakeRedis，结果完全确定。
- 真实模式：需要 API Key，使用真实模型与真实 Redis；同一任务可能产生 1~5 次 LLM 调用，延迟与 token 受供应商影响。
- 两份报告不能直接比较：mode 字段不同，且 Mock 的延迟只反映本地链路。对比时应固定同一 mode。

## 6. Langfuse 上传策略

- 未配置（`is_enabled()` 为 False）：`upload_dataset` / `upload_scores` 立即返回 0，无网络调用。
- dataset item 以 `task_id` 为 id；已存在则跳过。原因：Langfuse 对重复 item 会覆盖 description，导致历史任务 notes 被重跑覆盖。
- 每个任务上报 3 个 score：`pass_rate`（0/1）、`latency_ms`、`llm_calls`；trace_id 由最后一次请求的 `X-Request-ID` 派生。
- 任何异常都被吞掉并记录 `langfuse_eval_failed`，上传失败不影响评测完成。

## 7. `_call_llm` 的 Token 字段

改造前没有 `prompt_tokens` / `completion_tokens`，本步骤已补齐：
- 成功：从 `result.usage` 读取，缺失或字段非法时给 0；
- 失败：没有 usage，显式记录 0/0；
- 只新增字段，不改变既有字段、事件顺序与返回值。

## 8. 验证结果

| 项目 | 结果 |
|---|---|
| `pytest tests/` | **363 passed**（原 341 + 新增 22） |
| Mock 评测 | 27 个任务，执行 22，通过 22，跳过 5，通过率 100%，P50 7.9 ms，P95 15.93 ms，缓存命中率 13.64%，成本未配置单价 |
| 报告文件 | `reports/eval-<时间>-mock.json` 与 `.md` 成对生成 |
| 数据文件 | 评测使用内存数据源，`data/orders.json` 未被修改 |

## 9. 遗留与限制

- 暂不支持并发评测：并发会引入排队延迟，P95 不可解释；并发压测属于下一步。
- 成本单价需部署者按实际供应商填写，默认不计算成本。
- Langfuse dataset 版本管理未做：当前以 task_id 幂等，不区分任务集版本。
- MockLLM 不感知 Profile 工具子集，`tool_subset` 只能间接验证；严格子集约束需要在真实 LLM 模式下观察。
- 本次未实际运行 `--mode real`（环境无 API Key）；真实模式的指标需在配置 Key 后单独跑并另行记录。

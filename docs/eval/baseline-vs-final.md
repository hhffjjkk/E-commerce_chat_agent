# 改造前后对比报告

> 生成时间：2026-11-18；代码版本：`1d7ab38`；数据来源见每个表格的“来源”列，测量条件见 `docs/eval/measurement-conditions.md`。
> 所有数字都可在 `docs/resume/metrics-index.json` 按 `M-XX` 查到；没有量化来源的维度只做文字描述，不出现数字。

## 1. 执行摘要

多 Agent 路由把路由类任务通过率从 40.0%（M-03）提升到 100.0%（M-02，+60.0 个百分点，M-04）；归一化消息缓存在 8 并发下命中率 82.0%（M-05），QPS 从 516.29（M-08）提升到 890.92（M-09，+72.6%，M-06）；错误注入任务通过率 100.0%（M-13），但 Retry 与 Guardrails 的对照没有差异（M-14、M-15），只能做描述性说明。

## 2. 对比维度一览

| 维度 | 改造前 | 改造后 | 数据来源 | 测量条件 |
|---|---|---|---|---|
| 多 Agent 路由：路由类任务通过率 | 40.0%（M-03） | 100.0%（M-02） | B：`reports/baseline-no_multi_agent-*.json`、`reports/eval-*.json` | COND-05 |
| 多 Agent 路由：整体任务通过率 | 59.1%（M-17） | 100.0%（M-01） | B：同上 | COND-05 |
| 路由类任务通过率差值 | — | +60.0 个百分点（M-04） | B：M-02 与 M-03 之差 | COND-05 |
| 归一化消息缓存：评测通过率 | 关闭缓存 86.4%（M-16） | 开启缓存 100.0%（M-01） | B：`reports/baseline-no_nmsg_cache-*.json`、`reports/eval-*.json` | COND-02 |
| 缓存命中率 | 无缓存（N/A） | 82.0%（M-05） | B：`reports/load-*.json` | COND-01 |
| 并发 QPS | 516.29（M-08） | 890.92（M-09） | B：同一份 `load-*.json` | COND-01 |
| QPS 变化 | — | +72.6%（M-06） | B：M-08、M-09 计算 | COND-01 |
| P95 延迟 | 19.5 ms（M-18） | 19.0 ms（M-19） | B：同一份 `load-*.json` | COND-01 |
| P95 变化 | — | 降低 2.7%（M-07） | B：M-18、M-19 计算 | COND-01 |
| 错误注入任务通过率 | Retry 关闭 100.0%（M-14） | 100.0%（M-13） | B：`baseline-no_retry-*.json`、`eval-*.json` | COND-02 |
| 关闭两阶段确认后的错误注入通过率 | — | 100.0%（M-15） | B：`baseline-no_guardrails-*.json` | COND-04 |
| 工具子集越权 | 无量化对比 | 无量化对比 | C：见 3.5 | COND-05 |
| 错误响应统一性 | 无量化对比 | 无量化对比 | C：见 3.3 | COND-02 |
| Pending 跨实例一致性 | 无量化对比 | 无量化对比 | C：见 3.6 | COND-02 |
| LLM Token 成本 | 无量化对比 | 无量化对比 | C：见 3.7 | COND-02 |

## 3. 逐维度详述

### 3.1 多 Agent 路由与工具子集

- **改造前**：阶段一基线（COND-03）是单 Agent；所有请求共享同一套工具，没有 Profile、Router 与工具子集概念。
- **改造后**：`Router` 采用“规则 → LLM → 默认 Profile”三级降级；三个 Profile（pre_sales / order / after_sales）各自持有工具子集；`route_selected` 记录 agent/strategy/reason/confidence。
- **数据对比**：

| 指标 | 单 Agent 对照（B） | 当前多 Agent | 差值 | 来源 | 测量条件 |
|---|---|---|---|---|---|
| 路由类任务通过率 | 40.0% | 100.0% | +60.0 个百分点 | M-03 / M-02 / M-04 | COND-05 |
| 整体任务通过率 | 59.1% | 100.0% | +40.9 个百分点 | M-17 / M-01 / M-22 | COND-05 |

- **测量条件**：COND-05。单 Agent 对照通过“强制所有请求使用默认 Profile”实现；MockLLM 不感知 Profile 工具子集，因此该数字衡量的是**路由正确性**，不是工具权限差异。
- **工具子集**：该维度无量化对比。原因：MockLLM 直接按关键词调用工具，不读取 Profile 的 `tool_names`；真实 LLM 模式因无 API Key 未运行，无法统计越权调用次数。行为差异：改造后每个 Profile 只下发自身工具 schema，写工具只出现在 after_sales。

### 3.2 归一化消息缓存

- **改造前**：阶段一基线没有消息缓存（COND-03 明确记录“无语义缓存”）。
- **改造后**：`NmsgCache` 以“稳定前缀哈希 + 归一化消息哈希”为 key，按 Profile 白名单启用，order 使用更短 TTL；命中时跳过 `bot.reply`。
- **数据对比**：

| 指标 | 关闭缓存 | 开启缓存 | 变化 | 来源 | 测量条件 |
|---|---|---|---|---|---|
| 评测通过率 | 86.4% | 100.0% | +13.6 个百分点 | M-16 / M-01 / M-23 | COND-02 |
| 缓存命中率 | N/A | 82.0% | — | M-05 | COND-01 |
| 8 并发 QPS | 516.29 | 890.92 | +72.6% | M-08 / M-09 / M-06 | COND-01 |
| 8 并发 P95 | 19.5 ms | 19.0 ms | 降低 2.7% | M-18 / M-19 / M-07 | COND-01 |

- **测量条件**：评测为 COND-02；并发为 COND-01。
- **解释**：关闭缓存时通过率下降，是因为任务集中包含明确要求“第二次请求命中缓存”的任务；这不是一般意义上的正确性回退。并发绝对 QPS 受 10ms 模拟编排耗时影响，只用于相对比较。

### 3.3 Retry 与错误处理

- **改造前**：阶段一基线只记录“未触发 5xx、未出现工具失败”，没有统一的错误码与错误响应结构，也没有故障注入（COND-03）。
- **改造后**：错误码枚举 + 统一错误响应体 + `error_raised` 事件；Retry 默认关闭；`error_path` 任务注入工具异常、LLM 超时与路由异常。
- **数据对比**：

| 指标 | 对照 | 当前 | 来源 | 测量条件 |
|---|---|---|---|---|
| error_path 任务通过率 | `no_retry` 100.0% | 100.0% | M-14 / M-13 | COND-02 |
| error_path 任务通过率（关闭两阶段确认） | 100.0% | 100.0% | M-15 / M-13 | COND-04 |

- **测量条件**：COND-02、COND-04。
- **结论**：Retry 在当前默认配置下就是关闭的，`no_retry` 与当前完全一致，因此**没有可量化的 Retry 收益**；关闭 Guardrails 在 Mock 任务集下也没有差异，因为 MockLLM 不会发起写工具。统一的错误响应结构属于代码事实，不做数字对比。

### 3.4 并发与延迟

- **改造前**：阶段一只有单请求延迟记录（样本量小、客户端与启动方式不同，见 COND-03），没有并发 QPS/P95，不能直接比较。
- **改造后**：同一消息池、同一种子、仅切换缓存，得到：

| 指标 | 无缓存 | 有缓存 | 来源 | 测量条件 |
|---|---|---|---|---|
| 8 并发 QPS | 516.29 | 890.92 | M-08 / M-09 | COND-01 |
| 8 并发 P95 | 19.5 ms | 19.0 ms | M-18 / M-19 | COND-01 |
| 缓存命中率 | N/A | 82.0% | M-05 | COND-01 |

- **测量条件**：COND-01。
- **边界**：in-process ASGITransport、FakeRedis、关闭限流与 INFO 日志、cache-miss 注入 10ms 模拟编排耗时；**不包含**真实网络栈与真实 LLM。绝对 QPS 不代表生产容量。

### 3.5 工具子集越权

该维度无量化对比。原因：MockLLM 不感知 Profile 的 `tool_names`，无法观察“越权调用”；真实 LLM 模式未运行。行为差异：改造后工具 schema 按 Profile 下发，写工具只出现在 after_sales；`no_write_tools` evaluator 在所有 Mock 任务中均为通过。

### 3.6 Pending 跨实例一致性

该维度无量化对比。原因：当前评测与压测都在单进程内完成，无法测量跨实例读取一致性。行为差异：阶段五实现了 `RedisPendingStore` 与分布式锁，pending 记录可在多实例间共享；并发 confirm 通过锁串行化，Redis 不可达时显式失败而不是自动回退。

### 3.7 LLM Token 成本

该维度无量化对比。原因：本环境没有 API Key，评测与压测都运行 MockLLM，单任务 LLM 调用次数为 0（M-10），因此没有真实 Token 成本数据；阶段一基线同样没有真实 LLM 数据。行为差异：`llm_call(phase=end)` 已补齐 `prompt_tokens` / `completion_tokens`，配置真实单价后可直接产出成本。

## 4. 遗留与不再量化对比的维度

| 维度 | 状态 | 原因 |
|---|---|---|
| 真实 LLM 的 Token 与成本 | 未量化 | 无 API Key，Mock 不调用真实模型 |
| 真实网络栈 QPS / P95 | 未量化 | 压测为 in-process ASGITransport |
| 多进程 / 多 worker 吞吐 | 未量化 | 单进程评测环境 |
| 工具子集越权次数 | 未量化 | MockLLM 不读取 Profile 工具子集 |
| Retry 收益 | 未量化 | Retry 默认关闭，Mock 下无重试价值 |
| embedding 语义缓存收益 | 未实现 | 当前只有归一化精确哈希缓存 |
| 阶段一基线与本次数据的直接对比 | 不成立 | 客户端、服务启动方式、样本量与数据结构不同（COND-03） |

## 5. 复现方式

```bash
# 当前正式评测（COND-02 / COND-06）
python -m agent_system.eval.runner --tasks evals/tasks.jsonl --mode mock

# 并发压测（COND-01）
python scripts/run_load_test.py --scenarios cache_off cache_on_repeat_0.5 --mode mock

# 特性开关对照（COND-02 / COND-04 / COND-05）
python scripts/collect_baseline.py --variant no_multi_agent no_nmsg_cache no_retry no_router_llm no_guardrails --mode mock

# 生成数字来源索引
python scripts/summarize_metrics.py
```

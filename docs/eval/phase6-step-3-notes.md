# 阶段六步骤 3：改造前后对比与简历材料 — 实现说明

## 1. 数据来源盘点

| 对比维度 | 候选指标 | 数据来源 | 是否具备 |
|---|---|---|---|
| 正确性（路由） | 路由类任务通过率 | A：`docs/baseline/baseline-metrics.md`（只有单请求延迟，无路由指标）；B：`collect_baseline.py --variant no_multi_agent` + 当前 `eval-*.json` | B 具备，A 不足 |
| 正确性（整体） | 任务通过率 | B：同上 | B 具备 |
| 正确性（工具子集） | 越权工具调用次数 | B：`no_multi_agent` 对照 | **不具备**（MockLLM 不感知工具子集） |
| 错误处理 | 统一错误结构 / 故障注入通过率 | A：`baseline-run.md` 只有定性记录；B：`no_retry` / `no_guardrails` 对照；C：代码事实 | B 具备但无差异；结构统一性为 C |
| 并发吞吐 | QPS / P95 | B：步骤 2 的 `load-*.json`（`cache_off` 即无缓存基线） | 具备 |
| 缓存收益 | QPS 提升 / 命中率 / P95 | B：步骤 2 的 `cache_on_repeat_0.5` vs `cache_off`；评测通过率用 `no_nmsg_cache` 对照 | 具备 |
| Retry 收益 | error_path 通过率 | B：`RETRY_ENABLED=false` 与当前默认值一致 | 具备但无差异 |
| LLM Token 成本 | 单任务 Token / 成本 | B：`eval-*.json`（Mock 恒为 0） | **不具备真实数据** |
| Pending 跨实例一致性 | 跨 worker 读取 | C：描述性对比 | 仅描述性 |

关键判断：

- **来源 A 存在**，但它是 2026-11-09、commit `a7eaa37` 的 Mock 单 Agent 基线，只有单请求延迟与定性异常记录，**没有路由、缓存、QPS、P95**，因此不能用于这些维度的数字对比；报告中只把它作为历史架构描述。
- **来源 B 是主要依据**：`collect_baseline.py` 用特性开关跑当前评测器，`load-*.json` 提供缓存对照；两者都可重跑。
- **来源 C 只用于无法量化的维度**：工具子集越权、错误响应统一性、Pending 跨实例一致性、真实 LLM 成本。

## 2. 量化 vs 描述性维度

**有量化对比（全部来自 B）**：

| 维度 | 改造前 | 改造后 | 索引 |
|---|---|---|---|
| 路由类任务通过率 | 40.0% | 100.0% | M-03 / M-02 / M-04 |
| 整体任务通过率 | 59.1% | 100.0% | M-17 / M-01 / M-22 |
| 缓存关闭 vs 开启的评测通过率 | 86.4% | 100.0% | M-16 / M-01 / M-23 |
| 缓存命中率 | N/A | 82.0% | M-05 |
| 8 并发 QPS | 516.29 | 890.92 | M-08 / M-09 / M-06 |
| 8 并发 P95 | 19.5 ms | 19.0 ms | M-18 / M-19 / M-07 |
| error_path 通过率（Retry 关闭对照） | 100.0% | 100.0% | M-14 / M-13 |

**只有描述性对比（C）**：

- 工具子集越权：MockLLM 直接按关键词调用工具，不读取 `profile.tool_names`；真实 LLM 模式无 Key，未运行。
- 错误响应统一性：阶段一基线没有统一错误码；改造后有 `AgentError` + 统一响应体，这是代码事实，不是测出来的数字。
- Pending 跨实例一致性：单进程评测无法测量；改造后实现了 Redis pending 与分布式锁，属于架构差异。
- LLM Token 成本：Mock 模式调用量为 0（M-10），真实成本未采集。
- Retry 收益：Retry 默认关闭，`no_retry` 与当前配置一致，无法产生差异。

## 3. 对照实验设计

`scripts/collect_baseline.py` 复用步骤 1 的 `runner.run`，只改变一个开关：

| variant | 实现 | 用途 |
|---|---|---|
| `no_multi_agent` | 运行时替换 `app_module.select_profile`，所有请求走默认 Profile | 量化“无多 Agent 路由” |
| `no_nmsg_cache` | 在隔离环境进入后把 `nmsg_cache_enabled` 置 false | 量化“无缓存” |
| `no_retry` | `RETRY_ENABLED=false` | 与当前默认配置对照 |
| `no_router_llm` | `ROUTER_STRATEGY=rule_only` | 无 LLM 路由对照 |
| `no_guardrails` | `CONFIRMATION_ENABLED=false` | 仅用于对照，不是推荐配置 |

注意事项：

- 连续运行多个变体会触发进程内限流（120 次/分钟），采集脚本临时关闭限流并清空计数；
- `no_nmsg_cache` 需要覆盖步骤 1 Mock 环境“强制开启缓存”的默认行为；
- `no_guardrails` 的 100.0% 只表示“该 Mock 任务集下无差异”，不能说明生产可以关闭安全机制。

## 4. 报告生成方式

1. `scripts/collect_baseline.py` 产出 `reports/baseline-<variant>-*.json`，每份都带 `config_snapshot`（请求配置、生效配置、代码版本、注意事项）；
2. `scripts/run_load_test.py` 产出 `reports/load-*.json`；
3. `scripts/summarize_metrics.py` 只解析 JSON，按预定义指标清单生成 `docs/resume/metrics-index.json` 与 `.md`；
4. `docs/eval/baseline-vs-final.md` 手工整理，但**每个数字后面直接标注 M-XX**；写完后与 `metrics-index.md` 对照，避免手滑。

为什么不让对比报告全自动生成：报告需要解释“为什么这个维度不能量化”，自动模板容易隐藏缺失数据；因此数字自动汇总、叙述人工撰写，并用 M-XX 强制追溯。

## 5. 三层追溯链

```
简历/对比报告中的数字
  -> M-XX（docs/resume/metrics-index.json）
     -> source_file（reports/*.json）
        -> source_field（具体字段，如 by_category.route_rule_hit.pass_rate）
           -> condition（COND-XX，docs/eval/measurement-conditions.md）
```

任何一层缺失，数字就不能出现在简历或对比报告里。

## 6. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/scripts/test_collect_baseline.py` | 变体解析、`config_snapshot` 一致性、未知变体报错、monkeypatch 下不调用真实 LLM |
| `tests/scripts/test_summarize_metrics.py` | 全部预定义指标、缺失报告为 null 且有“数据缺失”、Markdown 与 JSON 一致、数字格式 |

`pytest tests/`：**394 passed**（步骤 1–2 的 385 项 + 本步骤 9 项），既有断言未修改。

## 7. 遗留

- 真实 LLM 模式的 Token 与成本对比未做（无 API Key）；
- 真实网络栈与多进程/多 worker 压测未做；
- 工具子集越权无法在 Mock 下量化；
- Retry 收益无法在默认关闭 + Mock 条件下量化；
- embedding 语义缓存的收益未量化（未实现）；
- 阶段一基线（COND-03）与本次数据测量方式不同，未做直接数字对比；
- `metrics-index.json` 已纳入版本控制；`reports/*.json` 仍被忽略，重跑报告后需重新生成索引。

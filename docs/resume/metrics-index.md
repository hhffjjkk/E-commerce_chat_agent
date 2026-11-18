# 数字来源索引

- 生成时间：2026-11-18T22:51:12
- 代码版本：1d7ab38

| ID | 指标 | 展示文本 | 来源 | 条件 | 注意事项 |
|---|---|---|---|---|---|
| M-01 | eval_pass_rate | 100.0% 任务通过率 | reports/eval-20261118-222230-mock.json | COND-02 | 当前代码、Mock 模式；跳过 only_real_llm 任务 |
| M-02 | route_rule_hit_pass_rate | 100.0% | reports/eval-20261118-222230-mock.json | COND-02 | 当前多 Agent 路由 |
| M-03 | route_rule_hit_pass_rate_no_multi_agent | 40.0% | reports/baseline-no_multi_agent-20261118-224702.json | COND-02 | 强制单一默认 Profile，模拟改造前单 Agent |
| M-04 | route_pass_rate_delta_pp | +60.0 个百分点 | reports/eval-20261118-222230-mock.json | COND-02 | 多 Agent 路由相对单 Agent 的路由类任务通过率差值 |
| M-05 | cache_hit_rate | 82.0% | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式；命中率包含首次集合的池子复用 |
| M-06 | cache_qps_gain_pct | 72.6% | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式；cache-miss 注入 10ms 模拟编排耗时，绝对 QPS 不代表生产容量 |
| M-07 | cache_p95_reduction_pct | 2.7% | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式；P95 可能受 miss 尾部影响 |
| M-08 | cache_off_qps | 516.29 QPS | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式，不能当作生产容量 |
| M-09 | cache_on_qps | 890.92 QPS | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式，不能当作生产容量 |
| M-10 | avg_llm_calls_per_task | 0 次/任务 | reports/eval-20261118-222230-mock.json | COND-02 | Mock 不调用真实 LLM，调用次数恒为 0 |
| M-11 | no_multi_agent_avg_llm_calls | 0 次/任务 | reports/baseline-no_multi_agent-20261118-224702.json | COND-02 | Mock 模式下与当前值相同，均不调用真实 LLM |
| M-12 | tool_subset_violations | — | C | COND-02 | MockLLM 不感知 Profile 工具子集；真实 LLM 模式未运行，无法量化越权调用 |
| M-13 | error_path_pass_rate | 100.0% | reports/eval-20261118-222230-mock.json | COND-02 | error_path 分类覆盖工具异常、LLM 超时与路由异常回退 |
| M-14 | no_retry_error_path_pass_rate | 100.0% | reports/baseline-no_retry-20261118-224703.json | COND-02 | Retry 默认关闭；该对照与当前配置一致 |
| M-15 | no_guardrails_error_path_pass_rate | 100.0% | reports/baseline-no_guardrails-20261118-224703.json | COND-02 | 关闭两阶段确认仅用于对照，不是推荐配置 |
| M-16 | no_nmsg_cache_eval_pass_rate | 86.4% | reports/baseline-no_nmsg_cache-20261118-225111.json | COND-02 | 关闭缓存后，期望命中缓存的 cache_hit 任务会失败；该值是缓存相关任务通过率差异 |
| M-17 | eval_pass_rate_no_multi_agent | 59.1% | reports/baseline-no_multi_agent-20261118-224702.json | COND-02 | 强制单一默认 Profile 后的整体任务通过率 |
| M-18 | cache_off_p95_ms | 19.5 ms | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式，不能当作生产容量 |
| M-19 | cache_on_p95_ms | 19.0 ms | reports/load-20261118-224022-mock.json | COND-01 | Mock 模式，不能当作生产容量 |
| M-20 | eval_task_count | 27 个任务 | reports/eval-20261118-222230-mock.json | COND-02 | 任务集规模；其中 only_real_llm 任务在 Mock 下跳过 |
| M-21 | load_total_requests | 650 次请求 | reports/load-20261118-224022-mock.json | COND-01 | Mock 并发压测的总请求数 |
| M-22 | overall_pass_rate_delta_pp | +40.9 个百分点 | reports/baseline-no_multi_agent-20261118-224702.json | COND-05 | 多 Agent 相对单 Agent 的整体任务通过率差值 |
| M-23 | cache_eval_pass_rate_delta_pp | +13.6 个百分点 | reports/baseline-no_nmsg_cache-20261118-225111.json | COND-02 | 开启缓存相对关闭缓存的评测通过率差值 |

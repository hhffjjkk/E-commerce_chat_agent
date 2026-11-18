# 测量条件索引

本文件集中记录每个数字的测量条件；对比报告与简历材料的每个数字都必须引用这里的 `COND-XX`。
只写数字不写条件，等于把结论从环境里剥离出来——这是不可追溯的。

---

### COND-01：Mock 模式并发压测

- 模式：Mock（不调用真实 LLM；`MockLLM` 为本地关键词规则）
- 环境：in-process `httpx.ASGITransport`，FakeRedis，线程安全内存会话库
- 限流：关闭（否则 120 次/分钟会触发 429）
- 日志：压测期间提升到 WARNING，避免结构化日志 I/O 主导 in-process 指标
- 模拟编排耗时：`MOCK_LLM_ORCHESTRATION_MS=10`（仅 cache-miss 请求，压测进程内）
- 并发度：1 / 4 / 8
- 每 worker 请求数：25
- 种子：42
- 消息池：`evals/load_messages.jsonl`（15 条）
- 预热：场景开始 5 个请求、每个并发度 `concurrency` 个请求，均不计入指标
- **不包含**：真实网络栈（TCP/TLS/代理）、真实 LLM 网络往返、真实 Redis 网络与单线程模型
- **适用结论范围**：只用于相对比较；绝对 QPS 受 10ms 模拟耗时与框架开销影响，不代表生产容量

### COND-02：特性开关对照评测

- 模式：Mock（不调用真实 LLM）
- 执行器：`agent_system.eval.runner.run`（与正式评测同一套逻辑）
- 任务集：`evals/tasks.jsonl`（27 条，8 分类；Mock 下执行 22 条，跳过 5 条 `only_real_llm`）
- 隔离：临时 SQLite、内存数据源（不写 `data/orders.json`）、FakeRedis
- 限流：采集脚本内临时关闭，避免连续变体触发 429
- 种子：42
- 对照方式：只切换一个特性开关或运行时选择逻辑，其余保持不变
- **不包含**：真实 LLM 的 Function Calling、Token 成本、真实网络延迟
- **适用结论范围**：任务通过率、路由类任务通过率、缓存相关任务通过率；不适用于生产容量或成本结论

### COND-03：阶段一历史基线（来源 A）

- 采集时间：2026-11-09
- 代码版本：`a7eaa37a60eff81696ad4a9e7baf74bd91c7610b`
- 客户端：Python `requests`，每次新建连接，`time.perf_counter()` 包住一次 `POST /chat`
- 服务：uvicorn 单进程，`127.0.0.1:8010`（SQLite）/ `8011`（Redis）
- 后端：`MockLLM`（无 API Key）
- 样本量：每个用例连续 3 次，使用不同 `session_id`；**n=3，不足以计算 P95**
- 文档：`docs/baseline/baseline-metrics.md`、`docs/baseline/baseline-run.md`
- **不包含**：并发、缓存、多 Agent 路由、故障注入、真实 LLM
- **适用结论范围**：只能作为历史架构与单请求延迟量级参考；**不与 COND-01/02 的数字直接对比**

### COND-04：`no_guardrails` 对照

- 在 COND-02 基础上设置 `CONFIRMATION_ENABLED=false`
- 目的：观察关闭服务端两阶段确认后评测结果是否变化
- **注意**：这是关闭安全机制的极端对照，不是推荐配置
- **不包含**：真实写操作（MockLLM 不会调用 `apply_refund` / `cancel_order`）
- **适用结论范围**：只能说明“在该 Mock 任务集下无差异”，不能说明生产环境可以关闭

### COND-05：`no_multi_agent` 对照

- 在 COND-02 基础上强制所有请求使用同一个默认 Profile（模拟改造前单 Agent）
- 实现：脚本临时替换 `app_module.select_profile`，并发出 `route_selected(reason=no_multi_agent)`
- **不包含**：真实 LLM 的工具子集约束（MockLLM 不感知 Profile 的 tool_names）
- **适用结论范围**：路由类任务通过率与整体任务通过率；**不能**用于量化工具越权

### COND-06：当前代码正式评测

- 模式：Mock
- 命令：`python -m agent_system.eval.runner --tasks evals/tasks.jsonl --mode mock`
- 任务集：27 条，Mock 下执行 22 条、跳过 5 条 `only_real_llm`
- 报告：`reports/eval-20261118-222230-mock.json`
- **不包含**：真实 LLM 调用、Token 成本、真实网络
- **适用结论范围**：任务通过率、分组通过率、错误注入任务通过率

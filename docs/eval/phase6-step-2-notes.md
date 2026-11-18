# 阶段六步骤 2：并发压测与 QPS/P95/命中率指标 — 实现说明

## 1. 职责分离与基线

| 维度 | 步骤 1：正确性评测 | 步骤 2：并发压测 |
|---|---|---|
| 输入 | `evals/tasks.jsonl`（27 条，有明确期望） | `evals/load_messages.jsonl`（15 条，无期望） |
| 目标 | 回答“行为是否正确” | 回答“吞吐、延迟与缓存收益如何” |
| 执行 | 串行、逐任务采集事件 | 并发、按响应提取指标 |
| 报告 | `reports/eval-*.json/.md` | `reports/load-*.json/.md` |

**复用步骤 1 的隔离环境**：`agent_system.eval.runner._EvalEnvironment` 提供临时 SQLite、内存数据源、FakeRedis 与 settings 覆盖；本步骤没有第二套隔离实现。`EvalCollector` 仅追加了 `task_summary(task_id)`，已有方法与字段不变。

## 2. 为什么用 asyncio + httpx，默认 in-process

- `httpx.ASGITransport` 直接把请求送进 ASGI app，不需要启动 uvicorn、管理端口或第二个进程；
- 与步骤 1 的工具链一致，CI 里一条命令即可跑完；
- 不引入 locust / k6 / wrk / pytest-benchmark 等依赖；
- 压测目标是“业务编排与缓存的并发表现”，不是真实网络栈；真实网络压测列为遗留。

## 3. 消息池（15 条）

| Profile | 条数 | 可重复 | 说明 |
|---|---|---|---|
| pre_sales | 6 | 5 | 商品价格/库存咨询（显示器、笔记本、键盘、台灯、鼠标、长消息） |
| order | 6 | 5 | 订单状态与物流轨迹（含 11 位订单号、快递单号） |
| after_sales | 3 | 0 | 投诉/售后咨询；after_sales 不在缓存白名单，因此不重复 |

- 总计 15 条（≥12），`repeat_friendly=true` 10 条（≥6），三个 Profile 各 ≥3；
- **不含写操作消息**：退款/取消会触发两阶段确认与数据变更，并发下的状态机正确性由步骤 1 的 `pending_flow` 覆盖；把写操作放进压测会引入确认失败与状态竞争，污染性能结论；
- 加载时会校验写操作关键词黑名单，出现即报错。

## 4. 场景（4 个内置，默认跑 2 个）

| 场景 | 缓存 | 重复比例 | 路径 | 用途 |
|---|---|---|---|---|
| `cache_off` | 关闭 | 0.0 | `/chat` | 无缓存基线 |
| `cache_on_repeat_0.5` | 开启 | 0.5 | `/chat` | 默认收益对比场景 |
| `cache_on_repeat_0.9` | 开启 | 0.9 | `/chat` | 高重复，接近纯内存响应上限 |
| `cache_on_stream_repeat_0.5` | 开启 | 0.5 | `/chat/stream` | 流式 TTFB 与整体延迟 |

- 默认并发度 `[1, 4, 8]`，每个 worker 25 个请求（`requests_per_level=0` 时按 `concurrency × 25` 计算）；
- 并发上限默认 8：in-process 模式共享同一事件循环与 GIL，继续增加并发度后延迟主要由调度而非业务决定；
- 默认不跑流式场景：ASGITransport 对分块响应的消费有额外开销，与 `/chat` 直接对比容易误读；需要时通过 `--scenarios` 显式启用。

### 4.1 请求序列生成

1. 先构造 `ceil(count × (1 - repeat_ratio))` 条首次消息（无放回；池子不足时循环取样）；
2. 重复消息从“首次集合 ∩ repeat_friendly”中有放回抽取；
3. 打乱顺序。

为什么重复消息要来自首次集合：若重复消息没有对应的首次请求，它在序列中只出现一次，缓存永远无法命中，命中率会系统性偏低。当请求数大于消息池时，首次集合本身也会循环复用，因此**实际命中率可能高于 `repeat_ratio`**（例如 0.5 注入 + 池子复用 → 82%）。

## 5. 指标口径与测量边界

| 指标 | 公式 | 边界 |
|---|---|---|
| QPS | 成功请求数 / 实际耗时 | 实际耗时 = 最后一个请求结束 − 第一个请求开始；错误请求不计入吞吐 |
| P50/P95/P99 | nearest-rank，成功请求的延迟 | 失败请求的耗时不计入；不插值 |
| 错误率 | 错误数 / 总请求数 | 非 2xx 或异常均记为错误 |
| 缓存命中率 | 命中请求数 / 总请求数 | 缓存关闭的场景显示 N/A；分母含失败请求 |
| 平均 LLM 调用 | 总调用数 / 总请求数 | Mock 模式恒为 0；响应协议未暴露真实调用次数 |
| TTFB | 第一行非空 NDJSON 到达时间 | 仅流式场景；无 TTFB 时显示“—” |
| LLM 节省估算 | 命中数 × 基线平均 LLM 调用 | 依赖 `cache_off` 基线；无基线返回“无法估算” |

**包含什么**：
- ASGI 调度、FastAPI 线程池、JSON 序列化、FakeRedis 字典读写；
- 规范化消息 lookups、稳定前缀哈希、会话状态写入。

**不包含什么**：
- 真实 TCP/HTTP 栈、TLS、操作系统网络调度；
- 真实 LLM 网络往返（Mock 模式）；
- 真实 Redis 的网络与单线程模型（FakeRedis 是进程内字典）。

### 5.1 Mock 模式的两项 benchmark 处理

1. **模拟编排耗时**：MockLLM 一次回复只要几十微秒，而 in-process 框架开销约 1ms，缓存节省会被完全淹没。为了让“缓存收益”在 Mock 下可测，压测器在 cache-miss 回复前注入固定 **10ms** 模拟编排耗时（`MOCK_LLM_ORCHESTRATION_MS`），并在报告运行信息与摘要中显式披露。它只作用于压测进程，不改变生产行为；**绝对 QPS 只用于相对比较，不代表生产容量**。
2. **关闭 INFO 日志**：压测期间把应用日志级别临时提升到 WARNING，避免结构化日志序列化/终端输出主导 in-process 指标；结束后恢复。生产 QPS 会因日志 I/O 低于报告值。
3. **关闭进程内限流**：默认限流 120 次/分钟，压测会迅速触发 429。压测期间临时把 `RateLimitMiddleware.enabled=False` 并清空计数桶，结束后恢复；否则会把部署策略误读为业务错误。

### 5.2 其他测量细节

- 时间戳使用 `time.perf_counter()`：Windows 下 `time.monotonic()` 的分辨率约 15.6ms，会把延迟量化成 0/16/31ms；
- 预热：每个场景开始前 5 个请求、每个并发度开始前 `concurrency` 个请求不计入指标；预热后清空缓存，保证测量阶段从冷缓存开始；
- 每个请求使用独立 `session_id`（`load-<scenario>-<concurrency>-<index>`），避免历史消息累积导致延迟漂移；
- 会话库替换为线程安全内存实现，避免 SQLite 单连接并发写锁竞争污染指标；
- 错误率 > 50% 时立即中止当前场景并记录 `aborted=error_rate_exceeded`，后续并发度不再执行，避免把故障当性能。

## 6. 缓存收益实验设计

- **同一消息池、同一种子、同一请求序列生成规则**；
- 仅切换 `cache_enabled` 与 `repeat_ratio`；
- 比较最高并发度下的 QPS、P95 与命中率；
- Mock 模式下 LLM 调用量为 0，QPS 提升来自“命中的请求跳过 cache-miss 模拟编排”；
- 真实模式下 LLM 调用量应显著下降，但本次环境无 API Key，未实际运行。

观测结果（Mock，8 并发，seed=42）：

| 场景 | QPS | P95(ms) | 命中率 |
|---|---|---|---|
| cache_off | 516.29 | 19.5 | N/A |
| cache_on_repeat_0.5 | 890.92 | 19.0 | 82.0% |

方向性结论：缓存场景 QPS 明显高于基线；P95 改善有限，因为 18% 的 miss 请求仍落在 10ms 模拟编排的尾部。该数据只说明链路方向，不代表生产容量。

## 7. 复现方式

```bash
# 默认：基线 + 50% 重复
python scripts/run_load_test.py --scenarios cache_off cache_on_repeat_0.5 --mode mock

# 全部场景（含流式）
python scripts/run_load_test.py --scenarios all --mode mock

# 覆盖并发度与采样量
python scripts/run_load_test.py --scenarios cache_off cache_on_repeat_0.9 \
  --mode mock --concurrency 1 4 8 --requests-per-worker 50 --seed 42
```

方向性断言（不硬编码 QPS）：
- `cache_on_repeat_0.5` 的缓存命中率 > 0；
- 最高并发度 QPS 不低于 `cache_off`；
- 错误率 > 50% 时 `aborted=error_rate_exceeded`；
- 压测前后 `data/orders.json` 的 SHA256 不变。

## 8. 测试

| 文件 | 覆盖 |
|---|---|
| `tests/eval/test_load_scenarios.py` | 场景参数、消息池约束、序列可复现与重复来源 |
| `tests/eval/test_load_metrics.py` | QPS、百分位、错误率、命中率、TTFB、节省估算 |
| `tests/eval/test_load_runner.py` | 低压强端到端、错误注入、中止保护、session 隔离、缓存清理、数据文件不变 |
| `tests/eval/test_load_reporter.py` | 报告区块、基线缺失、中止披露、JSON 结构、中文标签 |

`pytest tests/`：**385 passed**（步骤 1 的 363 项 + 本步骤 22 项），既有断言未修改。

## 9. 遗留

- 未做真实网络栈压测（TCP/TLS/代理）；
- 未做多进程 / 多 worker 压测；
- 未在真实 LLM 模式下验证流式 TTFB（需 API Key 与真实模型）；
- FakeRedis 是进程内字典，没有真实 Redis 的单线程模型、网络分区与超时行为；
- `llm_calls` 在压测中恒为 0：`/chat` 响应协议不暴露调用次数，本步骤未修改协议；
- Mock 绝对 QPS 受 10ms 模拟编排耗时与框架开销影响，不能作为生产容量；
- 阶段六步骤 3 将基于 eval 与 load 两类报告做改造前后对比与简历材料。

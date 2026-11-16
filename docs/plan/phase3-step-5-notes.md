# 阶段三 · 步骤 5 实现说明：Retry / Fallback 与重试策略

> 为 LLM 调用与幂等读工具引入可配置的指数退避重试，为 LLM 重试耗尽引入降级回复，
> 并用 `RetryObservingHook` 做异常出口观察。默认 `RETRY_ENABLED=false`，既有行为零变化。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增 | `harness/retry_policy.py`、`retry_executor.py`、`retry_targets.py` |
| 修改 | `errors/fallback.py`、`agent.py`、`tools.py`、`app.py`、`config.py`、`harness/__init__.py`、`.env.example` |
| 默认开关 | `RETRY_ENABLED=false`，不产生 `retry_*` 事件、无额外 sleep |
| 可重试目标 | LLM、`query_product` / `query_order` / `query_logistics` / `search_faq` |
| 永不重试 | `apply_refund` / `cancel_order` / `confirm_action` / `reject_action` |
| 降级 | 仅 `llm_failed` 且开关开启；工具/循环/确认失败不降级 |
| 测试 | 135 passed → **165 passed**（新增 30 项） |

## 2. 设计动机与默认关闭

- Retry 放在 Harness 调用层（`_call_llm` / `_tool_guard`），而不是中间件或异常处理器：
  只有这一层能区分“单次调用失败”与“整个请求失败”，也最接近真实恢复点；
- **默认关闭**的原因：
  1. 保证 135 项既有测试与线上行为零变化；
  2. 重试会放大上游故障，必须由运维显式评估后开启；
  3. 默认无 sleep，不会在不知情时拉长请求耗时。

开启后行为变化清单：

1. LLM 可重试错误最多尝试 `RETRY_MAX_ATTEMPTS` 次，产生 `retry_attempt` / `retry_exhausted`；
2. 幂等读工具失败后按同一策略重试；
3. `llm_call` 仍只记录一对 start/end，不随重试膨胀；
4. 重试耗尽且开启降级时，`/chat` 返回 200 + `fallback_text`；
5. `RetryObservingHook` 在异常出口额外记录 `retry_exhausted_observed`。

## 3. 白名单与副作用分析

| 工具 | 是否重试 | 理由 |
| --- | --- | --- |
| `query_product` / `query_order` / `query_logistics` / `search_faq` | 是 | 只读，重复调用无副作用 |
| `apply_refund` / `cancel_order` | **否** | 会修改 `orders.json`；若“已执行但响应丢失”，重试可能重复退款/取消，服务端无法安全区分 |
| `confirm_action` / `reject_action` | **否** | 两阶段状态机本身保证幂等，重试只增加并发压力 |
| LLM 调用 | 是 | 一次请求一次响应，天然幂等 |

写操作若未来要支持“安全重试”，需引入幂等键与去重表（超出本步骤范围，列入遗留）。

## 4. 退避与抖动

```text
base = initial_backoff_ms * multiplier^(attempt-1)
capped = min(base, max_backoff_ms)
backoff = round(capped * (1 + jitter_ratio * (2 * rand() - 1)))
```

- 默认 `300ms / ×2 / 上限 5000ms / 抖动 ±20%`；
- 指数退避用于应对上游短时拥塞，降低持续放大故障概率；
- 抖动避免多实例同时重试形成脉冲式负载；
- 封顶后才抖动，最坏不超过 `max_backoff_ms * (1 + jitter_ratio)`；
- `attempt<=0` 抛 `ValueError`；`rand` 可注入以便测试。

调优建议：429/超时较多的环境可先把 `RETRY_MAX_ATTEMPTS` 控制为 2~3、`INITIAL` 200~500ms，
并结合 `LOOP_TIMEOUT_SECONDS` 限制最坏耗时。

## 5. 流式边界

- `_call_llm(stream=True)` 的重试只覆盖**流对象创建**；
- 流迭代阶段（已开始 yield delta）失败**不重试**，直接走既有 `error` + `done(failed)` 协议；
- `reply_stream` 只在首个 chunk 之前捕获 `llm_failed` 并降级：
  产出 `delta(fallback_text)`，随后由业务生成器正常追加 `{"done":true}` 与扩展 `done(status=ok)`；
- NDJSON 结构未变，降级文本通过 `delta` 承载，不新增事件类型。

## 6. 降级语义

- 触发条件：`AgentError.code == llm_failed` + `RETRY_ENABLED=true` + `RETRY_FALLBACK_ENABLED=true`；
- `reply`：返回 `fallback_text`，HTTP 200；
- `reply_stream`：yield 一段 `fallback_text` delta；
- 其他错误（`tool_failed` / `datasource_failed` / `loop_*` / `confirmation_*` / `validation_error`）
  一律不降级，保持可诊断的错误响应；
- 事件：`degraded_reply`（`reason=llm_retry_exhausted`、`stream=true|false`）。

> 说明：既有错误码体系中 `llm_failed` 映射 HTTP **502**（不是 500），重试耗尽且未开启降级时
> 保持 502；本步骤未修改错误码表。

## 7. 与 LoopController 的边界

- LoopController：限制“一次请求最多多少轮 LLM 调用”（`max_steps`）；
- RetryExecutor：限制“单次调用失败后最多再试几次”（`max_attempts`）；
- 两者叠加的最坏耗时约为：

```text
max_attempts * max_steps * 单次调用耗时
```

生产必须同时设置 `LOOP_TIMEOUT_SECONDS` 收敛上界，否则开启 Retry 会显著拉长尾延迟。

## 8. 事件与 Langfuse

| 事件 | 字段 |
| --- | --- |
| `retry_policy_loaded` | 开关、四个退避参数、`retry_llm` / `retry_tools` / `fallback_enabled`、`policy_id`、`invalid_env`（不含 `fallback_text`） |
| `retry_attempt` | `target` / `attempt` / `max_attempts` / `error_code` / `backoff_ms` / `policy_id` |
| `retry_exhausted` | `target` / `attempts` / `last_error_code` / `policy_id` / `total_elapsed_ms` |
| `degraded_reply` | `reason` / `stream` |
| `retry_exhausted_observed` | `code` / `retryable` / `request_id` / `path` |

- `retry_exhausted` 由执行器在真正用完尝试时记录；
- `retry_exhausted_observed` 由异常出口的 `RetryObservingHook` 记录，不改变异常流向；
  两者职责不同，避免 Handler 层重复计数；
- `langfuse_mapping.py` 未修改，以上事件作为根 span metadata 的接入点（阶段六消费）。

## 9. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RETRY_ENABLED` | `false` | 总开关 |
| `RETRY_MAX_ATTEMPTS` | `3` | 含首次 |
| `RETRY_INITIAL_BACKOFF_MS` | `300` | 首次退避 |
| `RETRY_MAX_BACKOFF_MS` | `5000` | 单次上限 |
| `RETRY_BACKOFF_MULTIPLIER` | `2.0` | 指数因子 |
| `RETRY_JITTER_RATIO` | `0.2` | 抖动比例 |
| `RETRY_LLM` | `true` | 是否重试 LLM |
| `RETRY_TOOLS` | `true` | 是否重试幂等读工具 |
| `RETRY_FALLBACK_ENABLED` | `true` | 是否降级 |
| `RETRY_FALLBACK_TEXT` | `系统繁忙，请稍后重试。` | 降级文本（不写日志） |

非法值回退默认，并记录在 `retry_policy_loaded.invalid_env`。

## 10. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `get_retry_policy()` 默认值 | `False 3` |
| LLM 两次超时后成功 | 200；`llm_call` start/end 各 1；`retry_attempt` 2 |
| LLM 三次均超时（关闭降级） | 502 `llm_failed`；`retry_exhausted` 1 |
| 幂等读工具两次失败后成功 | `tool_call` 1 条、`retry_attempt` 2 条 |
| 写操作实现失败 | 单次执行、零 retry 事件 |
| LLM 耗尽 + 降级 | `/chat` 200，回复为 `fallback_text`，`degraded_reply` 1 |
| 工具失败 + 降级 | 仍返回 500 `tool_failed` |
| `/chat/stream` 首次耗尽 | `start → delta(fallback) → {"done":true} → 扩展 done(ok)` |
| `/chat/stream` 已 yield 后失败 | `error` + `done(failed)`，不降级 |
| 默认关闭 | 一次调用即失败，无 `retry_attempt` / `retry_exhausted` |

## 11. 测试

```text
pytest tests/ -q
165 passed in 1.26s
```

新增 30 项：

- `tests/test_retry_policy.py`：默认值、非法组合、env 解析/回退、policy_id、加载事件（不含 fallback_text）；
- `tests/test_retry_executor.py`：直通、成功重试、不可重试、耗尽、写工具白名单、抖动边界、sleep 参数；
- `tests/test_retry_integration.py`：LLM 重试/耗尽、读工具重试、写实现不重试、默认关闭回归；
- `tests/test_retry_fallback.py`：降级回复、关闭降级、工具失败不降级、流式两条边界、观察钩子。

既有 135 项全部通过，未修改既有断言。

## 12. 遗留问题

1. **429 与 RateLimit 的交互**：LLM 的 429 被映射为可重试；但本地 HTTP 限流（429 `rate_limited`）
   不在本次重试范围内，避免重试放大限流压力。
2. **写操作无幂等键**：当前写操作永不重试；若未来需要“已确认执行可重试”，需引入幂等键/去重表。
3. **多实例独立计数**：无全局熔断与跨进程重试协调，重试次数按进程计。
4. **Retry 不覆盖流迭代**：只在流对象创建阶段生效。
5. **降级文本静态**：没有按错误类型/语言细分，也未接入多语言。
6. **`retry_*` 未映射到 Langfuse span**：仅作为 metadata 接入点留给阶段六。
7. **`RETRY_FALLBACK_TEXT` 可包含业务信息**：已明确不写入日志，但需注意不要放敏感内容。
8. **Hook 与 executor 的双事件**：`retry_exhausted`（执行器）与 `retry_exhausted_observed`（出口）
   语义不同，分析时需分别统计，避免误判为重复。

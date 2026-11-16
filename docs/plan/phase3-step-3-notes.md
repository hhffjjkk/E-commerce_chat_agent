# 阶段三 · 步骤 3 实现说明：Loop Controller 与循环终止策略

> 把 `agent.py::_run_tool_loop` 中硬编码的“最多 5 轮”抽取为 `LoopController`：
> 轮次、工具调用次数、超时、外部中断四项约束统一由策略驱动。
> 默认配置下保持原行为；仅显式调整轮次时启用新的 `loop_exhausted` 错误语义。

## 1. 原 `_run_tool_loop` 行为基线（改动前实测）

| 维度 | 原实现 |
| --- | --- |
| 轮次上限 | `for _ in range(5)`，最多 **5 次** `chat.completions.create` 调用 |
| 退出条件 | 某次响应的 `message.tool_calls` 为空 → 立即 `return messages`（不追加该 assistant 消息） |
| 达到上限 | 5 轮后仍请求工具 → **静默返回**已有 messages；不抛异常、不追加兜底文本、不再发额外 LLM 调用 |
| 工具回填 | 先把 assistant 消息 `messages.append(msg)`，再对每个 tool_call 追加 `{"role":"tool","tool_call_id":call.id,"content":结果JSON}` |
| `finish_reason` | **未检查**，只依据 `tool_calls` 是否为空 |
| `stream` 差异 | 循环层始终使用非流式 `create`；`stream=True` 只在循环结束后创建流对象，循环逻辑无差异 |
| 工具异常 | 不捕获；`_tool_guard` 抛出的 `ToolExecutionError` / `DataSourceError` 直接冒泡到路由，返回统一 500 |
| 轮次计数 | 没有显式计数，按循环迭代次数隐式计数 |

## 2. Controller 设计

### 2.1 数据结构

- `LoopTerminationReason`：`completed` / `max_steps` / `max_tool_calls` / `timeout` / `interrupted` / `error`；
- `LoopPolicy`（frozen）：`max_steps=5`、`max_tool_calls=20`、`timeout_seconds=120`、`enable_interrupt_check=False`；
  `validate()` 要求 `max_steps>0`、`max_tool_calls>=max_steps`、`timeout_seconds>0`；
- `LoopState`：`policy` / `steps` / `tool_calls` / `started_at` / `terminated_reason` / `elapsed_ms`。

### 2.2 方法

| 方法 | 行为 |
| --- | --- |
| `begin()` | 初始化 state（`time.monotonic()`），记录 `loop_started` |
| `check(state, interrupt_probe=None)` | 按序检查 `steps >= max_steps` → `tool_calls >= max_tool_calls` → 超时 → 中断；命中抛 `AgentError` |
| `record_tool_call(state, tool_name=...)` | `tool_calls += 1`，不在此处拦截（允许最后一次工具执行完成） |
| `log_step(state)` | 每轮工具调用完成后记录 `loop_step` |
| `finish(state, reason)` | 写入 `terminated_reason` / `elapsed_ms`，记录一次 `loop_terminated`（幂等） |

`check()` 必须位于**发起下一次 LLM 调用之前**，因此 `max_steps=N` 精确等价于“最多 N 次 LLM 调用”；
`state.steps` 在 LLM 返回后递增，失败调用不占配额。

### 2.3 四个终止条件

| 原因 | 触发条件 | 默认是否可达 |
| --- | --- | --- |
| `completed` | 模型返回无 `tool_calls` | 是 |
| `max_steps` | `steps >= max_steps`（默认 5） | 是（原静默返回场景） |
| `max_tool_calls` | `tool_calls >= max_tool_calls`（默认 20） | 多工具并行时可达 |
| `timeout` | `monotonic() - started_at > timeout_seconds` | 默认 120s，正常请求不可达 |
| `interrupted` | 策略开启且探针返回 True | 默认关闭，不可达 |
| `error` | 循环内未捕获异常（含工具异常） | 是 |

## 3. 默认配置等价性

| 场景 | 重构前 | 重构后 |
| --- | --- | --- |
| 模型正常结束 | 返回 messages | 完全一致，额外记录 loop 日志 |
| 5 轮内完成工具调用 | 最多 5 次 create，按序回填 | 完全一致 |
| 默认 5 轮仍请求工具 | 静默返回 messages | **保留**：捕获 `loop_exhausted` 且 `max_steps==5` 时返回 messages |
| 工具异常 | 冒泡，统一 500 | 分类记录 `loop_terminated(reason=error)` 后原样冒泡 |
| `stream=True` | 循环后创建流 | 未改 |
| Mock 路径 | 不进入 loop | 未改，也不产生 `loop_*` 事件 |

**唯一有意的行为差异**：当用户显式配置 `LOOP_MAX_STEPS != 5` 且步数耗尽时，新实现抛
`loop_exhausted`（HTTP 500），而旧实现不存在该配置路径。需求明确要求该错误语义，
因此默认值保留旧行为、非默认值使用显式错误，避免静默截断。

## 4. 新错误码

| 错误码 | HTTP | retryable | 依据 |
| --- | --- | --- | --- |
| `loop_exhausted` | 500 | **False** | 策略上限是确定性的，重试只会再次命中 |
| `loop_timeout` | 504 | **True** | 超时多为上游暂时变慢，可交给阶段三后续 Retry |
| `loop_interrupted` | 503 | **False** | 外部取消/中断，重试无意义 |

- `details` 仅含安全字段：`reason`（仅耗尽）、`steps`、`tool_calls`；不含原始异常；
- 三者直接通过 `AgentError(code=...)` 构造，**未新增异常子类**：
  终止原因已经由 `details.reason` 表达，新增三个空子类只会扩大类型面；
  `handlers.py` / `fallback.py` 无需改动，统一处理器自然接管。

## 5. 循环内异常决策

按 4.1 基线：原实现**不捕获**工具异常。因此新实现也不吞：

- `AgentError`（工具失败、循环上限、超时、中断）→ `controller.finish()` 记录原因 → 除“默认 5 轮耗尽”外原样抛出；
- 其他 `Exception` → `finish(reason=error)` → 原样抛出；
- 工具异常在 `loop_terminated` 中体现为 `reason=error`，响应仍是原来的 `tool_failed` / `datasource_failed`。

## 6. 中断检查

- 默认 `LOOP_ENABLE_INTERRUPT_CHECK=false`，`check()` 完全不会调用探针（有测试用“调用即报错”的探针验证）；
- 开启后通过 `LLMClient.set_interrupt_probe(fn)` 注入无参回调；
- 阶段四多 Agent 可注入基于请求取消/租约到期的探针，无需改 Controller。

## 7. 事件与 Langfuse

| 事件 | 字段 |
| --- | --- |
| `loop_policy_loaded` | `policy_id`、四项策略、`invalid_env` |
| `loop_started` | `max_steps`、`max_tool_calls`、`timeout_seconds`、`policy_id` |
| `loop_step` | `step_index`、`elapsed_ms`、`tool_calls_so_far` |
| `loop_terminated` | `reason`、`steps`、`tool_calls`、`elapsed_ms`、`policy_id` |

`langfuse_mapping.py` 未修改；`loop_terminated` 作为根 span metadata 的接入点留给阶段三后续步骤 /
阶段六评测消费。`loop_terminated` 通过 `finish()` 幂等保证每次循环恰好一条。

## 8. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LOOP_MAX_STEPS` | 5 | 最多 LLM 调用轮次（与旧硬编码对齐） |
| `LOOP_MAX_TOOL_CALLS` | 20 | 单请求最多工具调用次数（必须 ≥ 轮次） |
| `LOOP_TIMEOUT_SECONDS` | 120 | 循环总时长上限（含 LLM 与工具耗时） |
| `LOOP_ENABLE_INTERRUPT_CHECK` | false | 外部中断检查开关 |

非法值回退默认并记录在 `loop_policy_loaded.invalid_env`。`config.py` 同步暴露 `loop_*` 字段，
`.env.example` 已追加注释。

## 9. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `get_loop_controller().begin().policy.max_steps` | `5` |
| fake 2 轮（1 工具 + 最终回复） | `steps=2`、`tool_calls=1`、`completed` |
| fake 5 轮持续工具调用 | create 5 次、静默返回、`reason=max_steps`（旧行为） |
| `LOOP_MAX_STEPS=2` | create 2 次后抛错；`/chat` 500 `loop_exhausted`、`retryable=false` |
| `LOOP_TIMEOUT_SECONDS=0.05` + 工具 sleep 0.1s | `/chat` 504 `loop_timeout`、`retryable=true` |
| 中断探针 True | `/chat` 503 `loop_interrupted`、0 次 LLM 调用 |
| 工具参数非法 | 抛 `tool_failed`，`loop_terminated.reason=error` |
| Mock 模式 `/chat` | 200，无任何 `loop_*` 事件 |

## 10. 测试

```text
pytest tests/ -q
111 passed in 1.12s
```

新增 20 项：

- `tests/test_loop_policy.py`（5）：默认、非法值、from_env 合法/回退、policy_id；
- `tests/test_loop_controller.py`（8）：begin、轮次/工具上限、超时、中断开关、计数、finish 幂等、
  log_step、单例重置；
- `tests/test_run_tool_loop_integration.py`（7）：正常完成、默认 5 轮兼容、自定义轮次 + HTTP 500、
  超时 504、中断 503、工具异常、Mock 无 loop 事件。

既有 91 项全部通过，未修改既有断言；`/chat`、`/chat/stream` 的 NDJSON 与响应结构未变。

## 11. 遗留问题

1. **默认 5 轮耗尽仍静默返回**：为兼容旧行为保留；如需统一为 500，可移除 `LEGACY_MAX_STEPS` 分支（属行为变更）。
2. **超时是轮询式检查**：只在每轮开始前判断，无法中断单次慢 LLM/工具调用；阶段三 Retry/超时治理再补。
3. **`max_tool_calls` 按单次调用计数**：一轮内多个 tool_call 会逐个累加，符合预期但需在运维文档说明。
4. **未接入 `finish_reason`**：保持原行为，只依据 `tool_calls`。
5. **`loop_*` 未映射到 Langfuse**：留作阶段三后续 metadata/评测接入点。
6. **`tool_name` 目前未写入 LoopState**：仅用于扩展点，诊断需结合 `tool_call` 日志。
7. **无跨进程共享计数**：Controller 为进程内单例，多 worker 各自独立。

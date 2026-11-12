# 阶段三 · 步骤 2 实现说明：Context Assembler 与上下文预算控制

> 将 `LLMClient._build_messages` 的简单拼接替换为可配置的 Context Assembler：
> token 预算、历史窗口、单条 user 截断、稳定前缀哈希。仅改变 messages 构造方式，
> 不改变 LLM 调用、工具 schema、API/NDJSON 结构与 Mock 路径。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增 | `harness/context_assembler.py`、`harness/context_policy.py` |
| 修改 | `agent.py`（`_build_messages` 委托）、`config.py`、`app.py`（`config_loaded` 增字段）、`.env.example`、`harness/__init__.py` |
| Token 估算 | 中文 0.6/字、其他 0.25/字符、每条消息固定 4 token；偏差约 ±30% |
| 稳定前缀 | `sha256(system + "\n" + 排序后的工具 schema)[:16]` |
| 测试 | 73 passed → **91 passed**（新增 18 项） |
| Mock 路径 | 不经过 Assembler（Mock 不做 LLM 消息构造），行为完全不变 |

## 2. 设计动机

原 `_build_messages` 只做 `system + 最近 20 条历史 + user` 的拼接：

- 没有 token 预算，长历史可能撑爆模型上下文；
- 没有单条消息上限，超长 user 输入会挤掉全部历史；
- system 与工具 schema 没有稳定指纹，阶段五语义缓存无法按“稳定前缀”命中。

Assembler 把这些问题收敛到一个可配置策略对象中；`memory.max_history` 仍是存储层上限，
组装层可独立配置更小的窗口，两者互不改动。

## 3. Token 估算口径

`estimate_tokens(text)`：

- 常用中日韩字符（`\u4e00`–`\u9fff`）按 **0.6 token/字**；
- 其他字符（英文/数字/标点）按 **0.25 token/字符**；
- 混合文本分别累加后**向上取整**。

`estimate_messages_tokens(messages)` 额外为每条消息加 **4 token** 固定开销。

误差与边界：

- 不追求与真实 tokenizer 一致，约 ±30% 误差，仅用于预算控制；
- 不计算 `tools=` schema 的体积（工具通过独立参数传递）；
- 真实用量以 LLM 返回的 `usage.prompt_tokens` / `usage.completion_tokens` 为准（阶段三后续消费）；
- `reserve_completion_tokens` 同样是估算值，不是硬保证。

## 4. 组装流程（按序执行）

1. **单条 user 截断**：超过 `max_user_message_chars` 时保留前 N 字符并追加 `"...[已截断]"`，
   置 `truncated_user=True`。先截断才能准确评估后续预算。
2. **稳定前缀哈希**：system 逐字参与；工具 schema 按 `function.name` 排序后
   `json.dumps(sort_keys=True, ensure_ascii=False)`；`sha256(system + "\n" + tools_str)[:16]`。
   工具描述**不注入 messages**，避免重复消耗 token。
3. **窗口裁剪**：`history[-max_history_messages:]`，保持原始顺序。
4. **预算裁剪**：`budget = max_total_tokens - reserve_completion_tokens`；
   从最旧历史逐条丢弃，直到估算 ≤ budget 或剩余条数 = `min_keep_recent_messages`；
   计算预算时已包含压缩标记消息。
5. **极小预算兜底**：达到保底条数仍超预算时，不再丢历史，改为二分查找“最长且不超预算”的
   system 前缀，尾部追加 `"..."`。此时 system 不再逐字保持（仅极端配置触发）。
6. **压缩标记**：`dropped > 0` 且开关开启时，在 system 之后插入
   `{"role":"system","content":"[上下文已压缩：早期 N 条对话已省略]"}`。
7. **组装**：`[system] + (marker?) + kept_history + [user]`，随后记录 `context_assembled`。

顺序不可调换：marker 不参与哈希、system 正常不参与裁剪，都是为了让缓存键只由
“system + 工具定义”决定。

## 5. 稳定前缀与阶段五衔接

`stable_prefix_hash` 的设计目标：

- 与 `dropped` 数量无关（marker 不参与）；
- 与 `tools_schemas` 传入顺序无关（先按 name 排序）；
- 仅 system 文本或工具定义变化时改变。

阶段五语义缓存可以将其作为 Key 前缀的一部分：
`cache_key = stable_prefix_hash + normalized_user_message`（具体实现留给阶段五）。
本步骤**不**把该字段注入 NDJSON，也不新增缓存逻辑。

## 6. `context_assembled` 事件

| 字段 | 说明 |
| --- | --- |
| `history_in` | 输入历史条数 |
| `history_out` | 实际保留条数 |
| `dropped` | 窗口 + 预算合计丢弃条数 |
| `truncated_user` | 单条 user 是否被截断 |
| `estimated_tokens` | 最终 messages 的估算 token |
| `stable_prefix_hash` | 16 位稳定前缀哈希 |
| `policy_id` | 策略字段哈希前 8 位，供阶段六策略对比 |

首次创建 Assembler 时还会输出一条 `context_policy_loaded`（含全部策略值与 `invalid_env`）。

Langfuse 消费：`langfuse_mapping.py` 当前只处理 request/tool/llm/error/rate 事件，
**未** 映射 `context_assembled`；按需求本步骤不修改映射层，留作阶段三后续接入点
（可作为根 span metadata，不新增子 span）。

## 7. 配置

`config.py` 新增 `context_*` 字段，`ContextPolicy.from_env()` 直接读取同一批环境变量
（便于测试中 monkeypatch 环境后立即生效，不受 `get_settings()` 缓存影响）：

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CONTEXT_MAX_HISTORY` | 20 | 组装层历史窗口 |
| `CONTEXT_MAX_TOTAL_TOKENS` | 8000 | 总预算（近似） |
| `CONTEXT_RESERVE_COMPLETION` | 1500 | 输出预留 |
| `CONTEXT_MIN_KEEP_RECENT` | 4 | 保底保留条数 |
| `CONTEXT_MAX_USER_CHARS` | 4000 | 单条 user 字符上限 |
| `CONTEXT_STABLE_PREFIX` | true | 是否把工具 schema 纳入稳定前缀 |

非法值回退默认，并在 `context_policy_loaded.invalid_env` 中列出，便于发现拼写错误。
`.env.example` 已追加对应注释。

## 8. 与 `agent.py` 的接入

- `_build_messages` 保留原方法名与签名，内部调用 `get_context_assembler().assemble(...)`，
  并把结果缓存在 `self._last_assembled`；
- `reply` / `reply_stream` / `_run_tool_loop` / `_call_llm` 均未改动；
- **Mock 路径不触发 Assembler**：`MockLLM.reply` 基于规则直接调用工具，不构造 LLM messages；
  因此 Mock 模式下的 `/chat` 不会产生 `context_assembled`，这是预期行为；
- LLM 路径可用 fake client 验证：首次调用会记录 `context_policy_loaded` +
  `context_assembled`，随后才是 `llm_call`。

## 9. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `get_context_assembler().assemble("S", [], "hi").messages` | `[{"role":"system","content":"S"},{"role":"user","content":"hi"}]` |
| 30 条历史 / 窗口 20 / 预算 8000-1500 | `history_out=20`、`dropped=10`、marker 存在、`estimated_tokens ≤ 6500` |
| 稳定前缀 | 工具顺序打乱 hash 不变；system 改一字 hash 变化；dropped 变化 hash 不变 |
| user 截断 | 10 字符上限 + 20 字输入 → 尾截断标记，`truncated_user=true` |
| fake LLM 路径 | 日志顺序 `context_policy_loaded → context_assembled → llm_call`，messages 首条为逐字 system |
| `SYSTEM_PROMPT` | sha256 `9c1e0c0a…bc4a85` 与重构前一致 |

## 10. 测试

```text
pytest tests/ -q
91 passed in 0.77s
```

新增 18 项：

- `tests/test_context_policy.py`（7）：默认合法、预算/保底校验、正整数校验、环境变量生效、
  非法值回退与 `invalid_env`、policy_id 变化；
- `tests/test_context_assembler.py`（11）：token 口径、基本组装、窗口裁剪与 marker、
  30 条历史验收、预算裁剪、极小预算 system 截断、user 截断、稳定前缀三项性质、
  日志字段、`_build_messages` 接入、SYSTEM_PROMPT 哈希。

既有 73 项全部通过，未修改任何既有断言。

## 11. 遗留问题

1. **Mock 路径不产生 `context_assembled`**：Mock 不做 messages 组装；若需要 Mock 也评测上下文，
   需另建模拟消息构造，非本步骤范围。
2. **估算误差**：未接入真实 tokenizer，±30% 误差；真实 usage 留待阶段三后续消费。
3. **system 截断无显式标记字段**：`AssembledContext` 按需求字段固定，未增加
   `system_truncated`；仅在内容尾部 `...` 可观察。
4. **tools schema 体积未计入预算**：当前只估算 messages 文本；工具很多时预算会偏乐观。
5. **`context_assembled` 未映射到 Langfuse**：留作阶段三后续接入（metadata 级）。
6. **策略单例缓存环境变量**：运行时修改环境变量需重启或调用测试用 reset；无热更新。
7. **`policy_id` 仅覆盖策略字段**：不含模型名/工具集合，阶段六对比评测时需与 stable hash 联合使用。

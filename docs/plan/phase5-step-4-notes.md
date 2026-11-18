# 阶段五步骤 4：稳定前缀归一化消息缓存与命中统计

## 0. 命名澄清（简历口径）

本步骤实现的是 **归一化消息缓存（Normalized Message Cache）**，不是 embedding 语义缓存：

- 命中条件：`stable_prefix_hash + normalized_msg_hash` **完全相等**；
- 不做同义句/近义句相似度匹配（那需要 embedding 模型 + 向量库 + 阈值评估）；
- 简历写法：**“基于稳定前缀的归一化消息缓存”** 或 **“精确匹配 + 字符级归一化”**；
- 不要写“语义缓存”——面试官会追问 embedding 模型与相似度阈值。

真正的语义缓存列为遗留，属于阶段六之后的可选增强。

## 1. 改动前基线（读代码记录）

| 项目 | 实际实现 |
|---|---|
| `stable_prefix_hash` | `ContextAssembler._stable_prefix_hash`：`sha256(system_prompt + "\n" + sorted(tools_schemas) 的 JSON)[:16]`；`stable_prefix_mode=False` 时工具部分为空 |
| `_last_assembled` | `_build_messages` 每次 LLM 调用前重建；对象**没有记录 Profile 归属** |
| `_call_tool` | 已改为 `get_registry().call(tool_name, arguments=...)`；可执行前查 `ToolSpec.tags` |
| `emit_cache` | 已存在 `emit_cache(hit, layer, key_prefix=None)`，`layer` 类型为 `Literal["session","semantic"]` |
| `schema.cache` | 必填 `event/request_id/ts/seq`，可选 `hit/layer/key_prefix/trace_id/span_id` |
| `/chat` 响应 | `{reply, route}`；`route` 为 `{agent, display_name, strategy, confidence, reason}` |
| `/chat/stream` 事件顺序 | `start → route → delta... → {"done":true} → 扩展 done`（失败时 `error → done(failed)`） |
| `select_profile` | 返回 `(profile, decision, reason)`；profile 一定非空 |

说明：`_last_assembled` 不用于查缓存——它可能来自上一次请求的另一个 Profile，
且没有归属标记，复用会造成跨 Profile 串缓存。

## 2. Key 布局与 TTL

| 用途 | Key | 值 | TTL |
|---|---|---|---|
| 归一化消息缓存 | `{prefix}:nmsg:{stable_prefix_hash}:{normalized_hash}` | 回复文本（非 JSON） | `pre_sales` 等：`NMSG_TTL_SECONDS=3600`；`order`：`NMSG_ORDER_TTL_SECONDS=60` |

- `normalized_hash = sha256(normalized_message)[:16]`，key 中不出现消息原文；
- `prefix` 为空时 key 为 `nmsg:{...}`（与其它缓存层一致，不额外加冒号）；
- **order TTL 更短**：订单状态会变化，缓存一小时会把“已发货”当“待发货”返回；
- **不实现主动失效**：订单状态变化时无法精确失效对应项，TTL 自然过期是唯一一致性保障。

## 3. 归一化规则

做什么：

1. `unicodedata.normalize("NFKC")` 统一全角/半角；
2. 中文标点（。、》《「」【】～—…）显式映射为半角；
3. 删除不可见字符（Unicode C*）与 emoji（So / 变体选择符）；
4. 英文/数字转小写；
5. 删除全部空白；
6. 连续重复句读标点折叠为一个（`？？？？` → `?`）。

**不做什么**（防误命中）：

- 同义词替换（“多少钱” ≠ “价格”）；
- 停用词剔除（“请问一下这个商品” ≠ “这个商品”）；
- 繁简转换；
- 词序调整（“退款申请” ≠ “申请退款”）。

长度边界：归一化后为空、长度 < `NMSG_MIN_LENGTH=4`、或纯数字/纯符号 → `is_cacheable=False`。

## 4. 写回条件（全部满足）

1. `NMSG_CACHE_ENABLED=true` 且 Profile 在白名单（默认 `pre_sales,order`）；
2. `used_tools` 不含带 `write` 标签的工具（`apply_refund` / `cancel_order`）；
3. `used_tools` 不含带 `internal` 标签的工具（`confirm_action` / `reject_action`）；
4. `reply` 非空；
5. 归一化消息通过 `is_cacheable`。

理由：

- 写操作结果被缓存会让用户看到“旧的成功”，而实际写操作并未执行；
- 确认流程涉及状态机，缓存会绕过两阶段确认；
- 短消息/纯数字没有复用价值，且容易误命中；
- 未调用任何工具的回复（纯 FAQ）可以安全缓存。

`used_tools` 来自 `LLMClient._last_used_tools`，在 `reply` / `reply_stream` 入口清空；
MockLLM 不实现该属性，app 用 `getattr(bot, "_last_used_tools", [])` 防御性取值，
因此 Mock 回复可能被缓存（本地演示无副作用）。

## 5. 接入点与事件顺序

### /chat

```
select_profile
→ _record_route_state
→（缓存开启时）_resolve_stable_prefix_hash → _nmsg_cache_lookup
   ├─ 命中：reply = cached（跳过 bot.reply）
   └─ 未命中：reply = bot.reply → _nmsg_cache_maybe_store
→ detect_escalation（对缓存回复同样生效）
→ 落历史
→ ChatOut(reply, route, cache={"hit","layer"})
```

- `cache` 为**新增可选字段**，键集固定 `{"hit","layer"}`，不含 `key_prefix`；
- 缓存关闭时使用 `response_model_exclude_none=True`，响应中**不出现**该字段。

### /chat/stream

```
start → route
→（缓存开启时）lookup
   ├─ 命中：cache(hit=true) → delta 分块 → {"done":true} → 扩展 done
   └─ 未命中：cache(hit=false) → 原有 start? 不重复 → delta... → done
```

- `cache` 事件在 `route` 之后、任何 `delta` 之前；
- 命中时按 `NMSG_STREAM_CHUNK_SIZE=20` 字符分块，保持打字机体验；
- 命中路径同样写历史（user + assistant），否则多轮上下文会丢失这一轮；
- 缓存关闭时**不发出** `cache` 事件，NDJSON 序列与步骤 3.5 完全一致；
- 流式失败/中断不写回缓存（写回在循环正常结束后执行）。

## 6. stable_prefix_hash 的复用与失效边界

- 计算口径与 `ContextAssembler` 逐字一致：Profile prompt + 排序后的工具 schema；
- 任一变化都会让缓存全部失效（这是设计意图）：
  - 改 `SYSTEM_PROMPT`；
  - 改某个 Profile 的 prompt；
  - 改 8 条工具 schema / 增删工具；
  - 改 `CONTEXT_STABLE_PREFIX` 开关；
- 不直接调用 `_build_messages`（会触发 Assembler 并消耗预算）；
- 不复用 `_last_assembled`（无 Profile 归属，可能串缓存）。

## 7. 事件表

| 事件 | 触发点 | 关键字段 |
|---|---|---|
| `cache_hit` | lookup 命中 | `profile`、`layer="nmsg"` |
| `cache_miss` | 全局开启但 Profile 不在白名单 / 未查到时 | `profile`、`layer`、`reason`（`not_enabled` / `not_found`） |
| `cache_write` | 成功写回 | `profile`、`layer`、`ttl_seconds` |
| `cache_write_skipped` | 不写回 | `reason`：`profile_not_in_whitelist` / `write_tool_used` / `internal_tool_used` / `reply_empty` / `not_cacheable` / `redis_error` |
| `cache_get_failed` | Redis 读失败 | `reason` |
| `cache_put_failed` | Redis 写失败 | `reason` |

隐私：不记录消息原文、归一化消息、`stable_prefix_hash`、key 前缀；
`cache_*` 事件显式 `session_id=None`，不把会话原文写进日志。

## 8. 与阶段六的衔接

- `cache_hit` / `cache_miss` / `cache_write` / `cache_write_skipped` 作为压测指标输入；
- 建议阶段六统计：命中率、命中节省的 LLM 调用量、命中/未命中的 P95 延迟；
- `cache_get_failed` / `cache_put_failed` 用于区分“缓存未命中”与“Redis 故障”。

## 9. 测试

| 文件 | 用例数 | 覆盖点 |
|---|---|---|
| `tests/test_nmsg_normalize.py` | 11 | 全角/空白/大小写/重复标点/emoji、纯数字/短消息/空消息、不做同义与停用词、稳定性 |
| `tests/test_nmsg_cache.py` | 7 | no-op、CRUD 与归一化等价命中、TTL 覆盖、前缀、哈希 key、降级事件、单例 |
| `tests/test_nmsg_integration.py` | 9 | 默认关闭零变化、miss→hit、流式命中顺序与文本、after_sales/write/internal/短消息跳过、Redis 故障、profile 哈希隔离、TTL 选择 |

`pytest tests/`：**341 passed**（314 既有 + 27 新增）。

**既有断言调整：0 条**。因为默认 `NMSG_CACHE_ENABLED=false`：
`/chat` 不输出 `cache` 字段、`/chat/stream` 不新增 `cache` 事件，既有 314 项行为完全不变。

## 10. 遗留

- **不做 embedding 语义缓存**：同义句/近义句不命中；引入 embedding 需要模型、向量库、
  相似度阈值与误命中评估，列为阶段六之后的可选增强；
- 不做繁简转换、同义词等价、停用词剔除、词序调整；
- 无主动失效：TTL 自然过期是唯一一致性保障；订单状态变化可能导致最多 60 秒旧答案；
- `MockLLM` 不记录 `_last_used_tools`，其回复可能被缓存（本地演示可接受）；
- `stable_prefix_hash` 复用 Assembler 口径，任何 prompt/工具 schema 改动都会让
  命中率瞬间归零，这是设计意图，但压测报告中需要注明变更时间点；
- Redis 不可达时每次请求都会尝试探活（受 5 秒 PING 缓存约束），并记录失败事件；
- 未引入 `fakeredis`，单测使用 mock 客户端；端到端使用临时最小 RESP 服务
  （支持 GET/SETEX/LIST，验证后已删除）。

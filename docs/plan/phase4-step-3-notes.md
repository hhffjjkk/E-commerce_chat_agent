# 阶段四 · 步骤 3 实现说明：Router 接入请求路径与 NDJSON `route` 事件

> 三个 HTTP 端点统一通过 `select_profile()` 决策子 Agent；`reply` / `_build_messages` /
> `_run_tool_loop` 接受 `profile` 并按 Profile 下发工具子集；`/chat` 响应新增 `route` 字段，
> `/chat/stream` 在 `start` 后立即发送 `route` 事件。前端未改动。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增 | `harness/selection.py`（`select_profile`） |
| 修改 | `app.py`、`agent.py`、`demo.py`、`stream/emitter.py`、`stream/schema.py`、`harness/__init__.py` |
| 决策顺序 | pending 粘滞 → Router → 默认 Profile |
| 工具子集 | `profile.tool_names` → `schemas_for()`；`profile=None` 保持全量 |
| NDJSON | `start → route → delta... → {"done":true} → 扩展 done` |
| `/chat` | 新增可选 `route` 字段；`reply` 不变 |
| 测试 | 212 passed → **237 passed**（新增 25 项） |

## 2. 为什么在端点层做 `select_profile`

- 路由依赖**会话**（pending 粘滞）与**请求消息**，两者在端点层最完整；
- 放在 `reply` 内部会让 Mock、CLI、webhook 各自重复实现，且 LLMClient 被迫感知 Guardrails；
- 放到 `harness/selection.py` 而不是 `app.py`：`demo.py` 需要复用同一套三级决策，
  避免“从 app 反向 import 辅助函数”。

## 3. 三级决策顺序

| 级别 | 条件 | `profile` | `decision` | `reason` |
| --- | --- | --- | --- | --- |
| pending 粘滞 | 会话存在未过期 pending action | `after_sales` | `None` | `pending_override` |
| Router | `get_router().route()` 返回已注册 agent | 该 agent | RouteDecision | `router` |
| 防御性回退 | Router 结果未注册 | 默认 Profile | RouteDecision | `profile_missing` |
| 异常回退 | Router 抛异常 | 默认 Profile | `None` | `router_exception` |

- `pending_override` 的 `decision=None`：粘滞是会话层领域规则，**不伪造** Router 决策；
- 默认 Profile 取 `RoutePolicy.default_profile`，不存在时取注册表第一个；
- `route_selection_failed` 事件只记录 `reason` / `error_type`，不记录堆栈。

## 4. `/chat` 响应变化

```json
{
  "reply": "...",
  "route": {
    "agent": "order",
    "display_name": "订单与物流",
    "strategy": "rule",
    "confidence": 0.6667,
    "reason": "router"
  }
}
```

- `route` 是可选扩展字段：旧客户端只读 `reply` 不受影响；
- pending 粘滞时 `strategy="pending_override"`、`confidence=1.0`；
- `route` 不包含用户消息原文或 LLM 原始响应；
- `reply` 内容与接入前一致（Mock 忽略 profile）。

## 5. NDJSON `route` 事件

```text
{"event":"start",...}
{"event":"route","agent":"order","display_name":"订单与物流","confidence":0.6667,
 "reason":"router","strategy":"rule","request_id":"...","ts":...,"seq":1}
{"delta":"..."}
{"done":true}
{"event":"done",...,"status":"ok"}
```

- `route` 在 `start` 之后、任何 `delta` 之前发出：前端可在首个 token 前更新 UI，
  且后续业务失败不影响已发出的路由信息；
- `emit_route` 新增**可选**字段 `display_name` / `strategy`，必填字段
  `event/request_id/ts/seq` 未变；`/chat/stream/schema` 同步追加这两个可选字段；
- `seq` 由 `StreamEmitter` 自增，顺序与发射顺序一致；
- 实现方式：在业务生成器前插入一行 `emit_route`，外层
  `wrap_streaming_generator` 仍先发 `start`，因此天然满足顺序。

## 6. 工具子集与降级

- `schemas_for(profile)` 逐个检查 `profile.tool_names`：
  - 已注册且启用 → 收集；
  - 缺失或 disabled → 记 `profile_tool_missing`（含 `profile` / `missing` / `fallback_to_all`）并跳过；
  - 过滤后为空 → 回退 `self.TOOLS` 全量，保证请求不因空工具列表失败；
- 选择“在此降级而非让 Registry 抛 ValueError”：Profile 是设计期配置，Registry 是运行期状态，
  设计期配置不应阻塞运行期请求；但缺失必须可观测，绝不静默；
- `_run_tool_loop` 只把子集传给 `_call_llm(tools=...)`，不改变 LoopController 上限；
- 若 LLM 幻觉调用子集外工具：`_call_tool` 在 Registry 查找失败，返回 `tool_failed`
  并带 `details.tool`，无需特殊处理。

实测：

| 消息 | 下发工具 |
| --- | --- |
| 这个商品多少钱 | `query_product`, `search_faq` |
| 我要退款 | `query_order`, `apply_refund`, `cancel_order`, `confirm_action`, `reject_action` |
| 我的订单到哪了 | `query_order`, `query_logistics` |
| profile=None | 全量 8 条 |

## 7. 稳定前缀按 Profile 隔离

`_build_messages(profile=...)` 使用 `compose_system_prompt(profile)`：
同一 Profile 的 `stable_prefix_hash` 稳定，不同 Profile 互异，为阶段五语义缓存按
Profile 分桶打基础；历史裁剪、预算、单条 user 截断逻辑不变。

## 8. Mock 与 CLI

- `MockLLM.reply/reply_stream` 接受 `profile` 但忽略：Mock 是规则驱动，与业务域无关；
- Mock 路径仍会经过 `select_profile`，因此 `/chat` 的 `route` 与 NDJSON `route` 事件照常输出；
- `demo.py` 复用 `select_profile`，输出 `[路由] 售前咨询（router）`，并以
  `bot.reply_stream(..., profile=profile)` 调用。

## 9. 事件与 Langfuse

| 事件 | 字段 |
| --- | --- |
| `route_selected` | `agent` / `display_name` / `strategy` / `reason` / `confidence` / `session_id`（自动）/ `latency_ms` |
| `route_selection_failed` | `reason` / `error_type` |
| `profile_tool_missing` | `profile` / `missing` / `fallback_to_all` |

- 均由 `log_event` 输出，不新建 span；`langfuse_mapping.py` 未修改；
- `reason` 与 `route_decided.reason` 口径一致，不含用户消息原文。

## 10. 启动注册顺序

`app.py`：

```python
register_builtin_tools()          # 先注册 8 个工具（Profile 校验依赖 write 标签）
register_builtin_agent_profiles() # 再注册 3 个 Profile
```

`demo.py` 同样按此顺序注册。两者均幂等。

## 11. 既有测试调整清单

| 文件 | 调整 | 原因 |
| --- | --- | --- |
| `tests/test_write_ops_confirmation.py` | 流式事件序列 `["start","done"]` → `["start","route","done"]` | 新增 route 事件 |
| `tests/test_errors.py` | `_FakeBot` 的 `_run_tool_loop` / `_build_messages` / `reply` / `reply_stream` 接受 `profile` 关键字 | 端点开始传 `profile=` |
| `tests/test_stream_error_event.py` | `_FailingBot.reply_stream` 接受 `profile` 关键字 | 同上 |
| `tests/test_retry_fallback.py` | 同上（内嵌 FailingBot） | 同上 |
| `tests/test_builtin_profiles.py` / `test_router.py` / `test_router_llm.py` | fixture 结束时恢复内置 Profile | 避免重置全局注册表后影响后续 `/chat` 测试 |

未放宽任何业务断言。

## 12. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `/chat` "我的订单到哪了" | 200，`route.agent=order`、`strategy=rule`、`reply` 正常 |
| `/chat` "我要退款" | `route.agent=after_sales` |
| `/chat` pending + "确认" | `agent=after_sales`、`reason=pending_override`、`strategy=pending_override`、`confidence=1.0` |
| `/chat/stream` 订单消息 | `start → route(order, rule) → delta... → done` |
| `/chat/stream` Mock 路径 | 同样输出 route 事件 |
| Router 抛异常 | 流仍完成，`route.reason=router_exception`、`agent=pre_sales` |
| 工具子集 | 三种 Profile 的下发列表与设计表一致；禁用工具时过滤且记录事件 |
| route 安全性 | 不含用户消息原文；`reply` 不变 |

## 13. 测试

```text
pytest tests/ -q
237 passed in 2.07s
```

新增 25 项：

- `tests/test_select_profile.py`（6）：Router 命中、pending 粘滞、过期不粘滞、profile_missing、
  router_exception、route_selected 事件；
- `tests/test_chat_route.py`（5）：order/after_sales 路由、pending 覆盖、reply 兼容与 route 安全性、
  webhook 路由；
- `tests/test_stream_route.py`（5）：事件顺序与字段、pending 覆盖、schema 新可选字段、
  Router 异常回退、Mock 路径；
- `tests/test_tool_subset_enforcement.py`（6）：三种 Profile 子集、禁用工具降级与事件、
  profile=None 全量、TOOLS property 全量；
- `tests/test_build_messages_profile.py`（4）：profile=None / profile 的 system prompt、
  稳定前缀按 Profile 隔离、历史裁剪仍生效。

既有 212 项回归通过。

## 14. 遗留问题

1. **pending 粘滞只处理 `pending`**：`confirmed` / `executed` 不粘滞；通用多轮 Profile 粘滞留待后续。
2. **前端未展示 route**：本步骤只暴露 NDJSON/响应字段，`static/index.html` 留给步骤 4。
3. **`route` 事件在极早失败时可能缺失**：`select_profile` 内部抛非预期异常时由流包装转 error；
   正常路径有兜底。
4. **`schemas_for` 每次请求重复计算**：Profile 工具数少，开销可忽略；后续可缓存。
5. **降级回退全量工具**：当 Profile 所有工具都不可用时返回全量，属于“可用优先”的保守策略，
   会临时放宽最小权限，需通过 `profile_tool_missing` 告警跟进。
6. **`route_selected` 在 selection 内记录**：与 prompt 中“端点内记录”略有差异，集中实现避免三处重复，
   字段与语义保持一致。
7. **`display_name` / `strategy` 为可选字段**：旧前端忽略即可，无需版本升级。

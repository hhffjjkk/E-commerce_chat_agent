# 阶段四 · 步骤 2 实现说明：Router 意图识别与路由决策

> 新增规则优先 + LLM 兜底 + 默认 Profile 兜底的三级路由。
> 本步骤只提供 Router 本体、事件与配置，**未接入** `/chat`、`/chat/stream`、`_build_messages` 与 NDJSON。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增 | `harness/route_rules.py`、`router_prompts.py`、`router.py` |
| 修改 | `harness/__init__.py`、`config.py`、`app.py`（仅 `config_loaded` 字段）、`.env.example` |
| 默认策略 | `rule_first`，阈值 `0.55`，默认 Profile `pre_sales` |
| 决策来源 | `rule` / `llm` / `fallback` |
| 行为影响 | 零：未接入任何请求路径，181 项既有断言未改 |
| 测试 | 181 passed → **212 passed**（新增 31 项） |

## 2. 为什么需要显式 Router

- 三个子 Agent 的工具与写权限不同，必须先把请求收敛到确定的业务域，才能下发正确的
  `tool_names` 与 prompt；
- 不做“Agent 之间自由对话”：多 Agent 互相转交会产生不可控的轮次、成本与延迟，
  且难以评估。Router 一次决策、失败收敛到默认 Profile，边界清晰；
- 规则路由零成本、零延迟、可解释，能覆盖大部分中文客服请求；LLM 只处理规则无法区分的
  长尾表达。

## 3. 三级降级路径

| 级别 | 触发条件 | 结果 |
| --- | --- | --- |
| 规则 | `best_rule` 命中且置信度 ≥ 阈值 | `strategy=rule`，`reason=rule_hit` |
| LLM | 规则低置信/未命中，且 `llm_enabled` + 客户端可用，解析出合法 agent 且置信度 ≥ 阈值 | `strategy=llm`，`reason=llm_hit` |
| 默认 Profile | 以上都失败（`no_profiles` / `no_llm` / `parse_error` / `below_threshold` / `llm_exception` / `invalid_default_profile`） | `strategy=fallback`，`agent=default_profile` |

策略变体：

| `ROUTER_STRATEGY` | 行为 |
| --- | --- |
| `rule_first`（默认） | 规则高置信直返；否则 LLM；LLM 失败/低置信时保留规则低置信结果；规则也无则默认 |
| `llm_first` | 先 LLM；未达阈值/失败回退规则；规则也无则默认 |
| `rule_only` | 只跑规则；未命中则默认 |
| `llm_only` | 只跑 LLM；失败则默认 |

`default_profile` 默认 `pre_sales`：售前请求最常见、无写副作用，作为兜底最安全。
若配置的默认 Profile 不在注册表，回退到注册顺序第一个，`reason=invalid_default_profile`。

## 4. 规则表与评分

| Profile | 关键词 |
| --- | --- |
| `pre_sales` | 商品、价格、多少钱、库存、有货、规格、尺寸、颜色、功能、推荐、哪个好、适合、参数 |
| `order` | 订单、物流、快递、单号、发货、到哪了、签收、配送、运单 |
| `after_sales` | 退款、退货、取消、售后、换货、补偿、投诉、申请退 |

- 每条规则 `weight=1.0`；`score_message` 每命中一个关键词加权重；
- 关键词跨 Profile 严格互斥，模块导入时执行 `validate_rule_table()`，违规抛 `RuntimeError`
  （设计期错误，不是用户输入错误）；
- `best_rule`：最高分并列或全 0 时返回 `(None, 0.0)`，交给下一级；
- 置信度 `score / (score + 1)`：单调递增且 < 1，避免“多命中几个词就给 1.0”的虚假高置信。
  单命中 = 0.5，双命中 ≈ 0.667。

**阈值 0.55 的依据**：单关键词命中略低于阈值，触发 LLM 复核或保留低置信规则结果；
双命中即达到阈值，可直接信任规则。这与“至少一个高权重信号或 LLM 中等以上确定”的直觉一致。

## 5. LLM 路由

- prompt 只包含子 Agent 列表与严格 JSON 输出要求，**不复用业务 `SYSTEM_PROMPT`**：
  路由是元任务，业务规则会干扰“选哪个域”；
- 调用方式：`chat.completions.create(model=..., messages=..., temperature=0.0,
  max_tokens=200, timeout=ROUTER_LLM_TIMEOUT_SECONDS)`，**不传 `tools=`**；
- **不重试**：路由失败已有三级降级，重试只会放大延迟，且与 `RetryPolicy` 的“调用级重试”语义不同；
- 解析容错：
  - 去除 ```json 围栏；
  - `raw_decode` 只取第一个 JSON 对象；
  - agent 不合法 → `invalid_agent`；
  - confidence 缺失给 0.5、越界夹到 `[0,1]`；
  - reason 缺失给空串；
  - 任何解析失败返回 `parse_error`，不抛异常。

LLM 客户端：优先构造参数注入；未注入且 `llm_enabled=True` 时延迟实例化 `LLMClient`，
无 API Key/导入失败都视为“LLM 不可用”，不阻塞 Router 构造。这样避免 Router 与 app
启动顺序强耦合。

## 6. 事件

| 事件 | 字段 |
| --- | --- |
| `router_policy_loaded` | `strategy` / `confidence_threshold` / `default_profile` / `llm_enabled` / `policy_id` / `invalid_env` |
| `route_decided` | `agent` / `confidence` / `strategy` / `reason` / `latency_ms` / `llm_used` / `candidates`（JSON，最多 3 项） |
| `route_llm_call` | `latency_ms` / `error_tag` / `raw_len` |

- `route_decided.reason` 只使用策略标签（`rule_hit` / `llm_hit` / `below_threshold` /
  `no_llm` / `parse_error` / `no_profiles` / `llm_exception` / `invalid_default_profile`），
  **不记录用户消息或 LLM 原文**；LLM 返回的 `reason` 仅在内部解析，不写入决策/日志；
- `langfuse_mapping.py` 未修改，这些事件作为根 span metadata 的接入点（步骤 4 / 阶段六）；
- 不向 NDJSON 追加任何事件。

## 7. 不接入请求路径

本步骤**没有**修改 `/chat`、`/chat/stream`、`/webhook`、`_build_messages`、`reply` /
`reply_stream`、`_call_llm`、`_run_tool_loop`、`SYSTEM_PROMPT`、8 条工具 schema、
3 个 Profile 字段。`app.py` 只在 `config_loaded` 事件中增加了 Router 配置字段（不实例化 Router）。

接入在步骤 3：`route` 结果将决定 `compose_system_prompt(profile)` 与
`registry.schemas(names=profile.tool_names)`，并把 `profile.name` 写入 NDJSON `route` 事件。

## 8. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ROUTER_STRATEGY` | `rule_first` | 四种策略 |
| `ROUTER_CONFIDENCE_THRESHOLD` | `0.55` | 低于阈值需复核/降级 |
| `ROUTER_DEFAULT_PROFILE` | `pre_sales` | 最终兜底 |
| `ROUTER_LLM_ENABLED` | `true` | LLM 路由开关 |
| `ROUTER_LLM_TIMEOUT_SECONDS` | `5` | LLM 路由超时 |

非法值回退默认并记录在 `router_policy_loaded.invalid_env`。

## 9. 验收与实测

| 检查 | 结果 |
| --- | --- |
| `我的订单到哪了` | `order` / `rule` / `rule_hit` / 0.6667 |
| `我要退款` | `after_sales` / `rule` / `below_threshold` / 0.5 |
| `这个商品多少钱` | `pre_sales` / `rule` / `rule_hit` / 0.6667 |
| `今天天气不错`（无 LLM） | `pre_sales` / `fallback` / `no_llm` / 0.0 |
| 规则高置信 | 不调用 LLM（fake 断言 0 次） |
| 规则低置信 + LLM 高置信 | `strategy=llm` |
| LLM 低置信 | 保留规则结果，`below_threshold` |
| LLM 异常/超时 | `llm_exception` / 回退规则或默认 |
| 无 Profile | `no_profiles` + 默认 Profile |
| 默认 Profile 不存在 | 首个 Profile + `invalid_default_profile` |

## 10. 测试

```text
pytest tests/ -q
212 passed in 1.87s
```

新增 31 项：

- `tests/test_route_rules.py`（10）：覆盖/互斥/未知 Profile/缺规则/典型消息/无关消息/并列/置信度单调；
- `tests/test_router.py`（14）：策略校验与 env、no_profiles、rule_only、llm_only 无 LLM、
  rule_first 高/低置信、LLM 低置信、llm_first 成功/回退、无效默认、事件字段、单例；
- `tests/test_router_llm.py`（7）：合法/围栏/非法 JSON/invalid agent、异常/超时分类、
  轻量调用参数（无 tools、temperature=0、max_tokens=200）、无客户端。

既有 181 项全部通过，未修改任何既有断言。

## 11. 遗留问题

1. **规则表人工维护**：新增 Profile 时必须同步扩充关键词并保持互斥，否则只能依赖 LLM。
2. **规则表仅覆盖中文**：英文、方言、错别字未覆盖，长尾依赖 LLM。
3. **LLM 置信度未校准**：模型自报 confidence 不等于真实准确率，阈值 0.55 是经验值，
   需阶段六用评测数据校准。
4. **单轮决策**：不做多轮澄清；模糊消息直接落到默认 Profile。
5. **未接入请求路径**：步骤 3 才消费 `RouteDecision` 与 NDJSON `route` 事件。
6. **`model_override` 未消费**：Router 暂不参与模型选择。
7. **`route_decided` 未映射 Langfuse**：作为根 span metadata 的接入点留待步骤 4 / 阶段六。
8. **候选得分在 LLM 路径下是占位值**：规则得分仍会传入 `candidates` 供诊断，
   但 LLM 路由的候选只包含最终 agent 与默认 Profile 占位，不能直接当作概率。

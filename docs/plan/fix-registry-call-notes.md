# 修复 Tool Registry 参数冲突（阶段三遗留 bug）

## 1. 缺陷复现与根因

**现象**

```text
>>> registry.call("query_product", name="极光笔记本")
TypeError: ToolRegistry.call() got multiple values for argument 'name'
```

`/chat` 与 `/chat/stream` 对“极光笔记本多少钱”等命中商品关键词的请求统一返回
`500 internal_error`；其他意图（订单/物流/售后）不受影响。

**根因**

旧签名为 `call(self, name: str, **kwargs: Any)`。调用 `call("query_product", name=...)` 时：

1. 第一个位置实参 `"query_product"` 绑定到形参 `name`；
2. 关键字实参 `name="极光笔记本"` 也试图绑定同一个形参 `name`；
3. Python 在函数体执行前就抛出 `TypeError`，`_tool_guard` 来不及把它映射为领域异常，
   最终被全局兜底成 `internal_error`。

商品查询是唯一入参名为 `name` 的工具，所以只有它暴露问题；这是“形参名与工具入参名共用
同一个命名空间”导致的必然冲突，而不是偶发错误。

## 2. 方案对比与选择

### 方案 A：只改形参名

```python
def call(self, tool_name: str, **kwargs: Any) -> Any:
    ...
    return spec.callable(**kwargs)
```

- 优点：改动最小，调用点 `registry.call(name, **args)` 可保持原样；
- 缺点：只是把冲突点从 `name` 挪到 `tool_name`。若未来某工具入参叫 `tool_name`，
  同样的 `TypeError` 会再次出现；没有从机制上消除“工具入参污染 Registry 形参”的可能。

### 方案 B：显式 `arguments` 字典（采用）

```python
def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
    ...
    return spec.callable(**(arguments or {}))
```

- 优点：
  - 工具入参只存在于字典内部，无论叫 `name` / `tool_name` / 其他名字都不可能再与形参冲突；
  - 与 OpenAI tools API 的 `function.arguments`（JSON 字符串 → 字典）语义一致；
  - 后续做工具调用审计、参数脱敏或缓存键计算，可在 `arguments` 层统一拦截；
- 缺点：破坏性变更，所有调用点必须同步修改。项目内 `registry.call` 调用点只有 6 处
  （见第 4 节），且 Registry 是内部组件，无外部消费者，改造成本可控。

**结论**：选择方案 B，不保留 `**kwargs` 兼容路径，避免“看起来修复、实际仍可复现”。

## 3. `ToolRegistry.call` 变更前后

| 项目 | 变更前 | 变更后 |
|---|---|---|
| 签名 | `call(self, name: str, **kwargs: Any)` | `call(self, tool_name: str, arguments: dict[str, Any] \| None = None)` |
| 参数键来源 | `sorted(kwargs)` | `sorted(arguments or {})` |
| 调用实现 | `spec.callable(**kwargs)` | `spec.callable(**(arguments or {}))` |
| 未注册 | `ToolExecutionError("工具未注册", tool=name, args_keys=...)` | 同左，仅首个形参改名 |
| 已禁用 | `ToolExecutionError("工具未启用", tool=name, args_keys=...)` | 同左 |
| docstring | 描述 `**kwargs` | 增加“为什么改为 arguments 字典”的设计说明 |

`register` / `unregister` / `get` / `list` / `schemas` / `snapshot` / `enable` / `disable`
均未改动；`ToolSpec`、`_validate_spec`、全局单例也未改动。

## 4. 调用点清单

用 `grep -rn "registry\.call\|\.call\(" agent_system/ tests/` 确认，全部 6 处：

| 文件 | 位置 | 变更 | 原因 |
|---|---|---|---|
| `agent_system/agent.py` | `MockLLM.reply` 物流分支 | `registry.call("query_logistics", tracking_no=...)` → `arguments={"tracking_no": ...}` | 新签名不再接受任意关键字 |
| `agent_system/agent.py` | `MockLLM.reply` 订单分支 | `arguments={"order_id": ...}` | 同上 |
| `agent_system/agent.py` | `MockLLM.reply` 商品分支 | `arguments={"name": key}` | 本次修复的关键路径 |
| `agent_system/agent.py` | `LLMClient._call_tool` | `call(name, **args)` → `call(name, arguments=args)` | 真实 LLM 工具调用的统一入口 |
| `tests/test_tool_registry.py` | `test_disable_and_enable_affect_schemas_and_call` | `call("demo_tool", value="x")` → `arguments={"value": "x"}` | 仅改调用形式，断言不变 |
| `tests/test_tool_registry.py` | 同上（启用后的断言） | 同上 | 同上 |

其他模块（`confirmation_tools.py`、`guardrails.py` 等）不直接调用 `registry.call`，
无需修改。`guardrails.confirm` 调用的是 `_apply_refund_impl` 等实现函数，不经 Registry，
因此 `arguments={"action_id": ...}` 只在 `confirm_action` 工具入口处展开。

## 5. 测试变更清单

### 新增 `tests/test_registry_call_param_conflict.py`（9 个用例）

| 用例 | 验证点 |
|---|---|
| `test_query_product_with_name_keyword` | `arguments={"name": "极光笔记本"}` 返回 `found=True` |
| `test_legacy_kwargs_form_is_rejected` | 旧 `name=...` 形式抛 `TypeError`，证明无兼容后门 |
| `test_all_tools_callable_via_arguments` | 8 个内置工具全部经 `arguments` 成功分发（写操作走两阶段确认并完成 confirm/reject） |
| `test_unknown_tool_raises_tool_execution_error` | 未注册工具仍抛 `ToolExecutionError`，`details` 口径不变 |
| `test_disabled_tool_raises_tool_execution_error` | 禁用工具抛 `ToolExecutionError`，`details.tool/args_keys` 不变 |
| `test_arguments_none_means_no_args` | `None` 与 `{}` 等价 |
| `test_llm_client_call_tool_uses_arguments_dict` | `_call_tool` 调用点正确分发 |
| `test_end_to_end_chat_product_query` | `/chat` 商品查询 200、含商品信息、无 `internal_error` |
| `test_end_to_end_stream_product_query` | `/chat/stream` 无 `error` 事件、有 delta、有 done |

写操作测试通过 `monkeypatch` 替换 `tools._source.apply_refund/cancel_order`，不修改仓库内
`data/orders.json`；读工具继续使用真实演示数据。

### 修改既有测试

仅 `tests/test_tool_registry.py` 的 2 处调用形式（见第 4 节），断言内容与强度不变。

### 回归

```text
237 passed（修复前）
246 passed（修复后，237 + 9 新增）
```

`tests/test_errors.py`、`tests/test_builtin_tools_migration.py` 均通过，证明异常映射与
8 条工具 schema / 注册数量未变化。

## 6. 端到端验证

启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn agent_system.app:app --host 127.0.0.1 --port 8013
```

`/chat`：

```text
POST /chat  {"session_id": "s1", "message": "极光笔记本多少钱"}
200
{"reply": "极光笔记本电脑 14：14英寸2.8K屏 16GB+512GB 轻薄本\n价格：¥5999\n库存：有货（剩余 35 件）",
 "route": {"agent": "pre_sales", "display_name": "售前咨询", "strategy": "rule", "confidence": 0.5, "reason": "router"}}
```

`/chat/stream`：

```text
200
{"event": "start", ...}
{"event": "route", ..., "agent": "pre_sales", "display_name": "售前咨询", ...}
{"delta": "极光笔记本电脑 14：..."}
{"done": true}
{"event": "done", ..., "status": "ok", ...}
```

修复前同样的请求返回 `500 {"error": {"code": "internal_error"}}`。

命令行断言：

```text
>>> get_registry().call('query_product', arguments={'name': '极光笔记本'})['found']
True
>>> get_registry().call('query_product', name='极光笔记本')
TypeError: ToolRegistry.call() got an unexpected keyword argument 'name'
```

## 7. 为什么这次修复不影响协议、schema、Profile 与路由

- 只改 Registry 的调用约定与 6 处调用点；
- 8 条工具 schema 仍来自 `tools.TOOL_SCHEMAS`，名称、描述、参数定义逐字未动；
- `AgentProfile.tool_names` 与工具子集过滤基于工具名，不感知 Registry 的形参；
- Router 只选择 Profile，不直接调用工具；
- NDJSON 事件结构、`/chat` 响应结构、错误码与 `ToolExecutionError.details` 字段均未变化；
- 前端 `static/index.html` 未改动。

## 8. 为什么在阶段五之前修复

阶段五会引入 Redis 双层缓存，第一批验证用例几乎必然包含商品查询（最典型、最易缓存）。
若带着这个必现 500 的缺陷进入缓存开发，会把“真实错误”和“缓存未命中/序列化问题”混在一起，
增大排障成本。先修复并补齐 8 个工具的分发回归，才能让缓存层在一个干净、可预期的基线上验证。

## 9. 遗留

- 当前 8 个工具都只接受关键字参数，因此 `arguments` 字典足够；若未来某个工具必须接收
  位置参数，`call` 需要显式支持 `*args`（或在工具内部做适配），本步骤不实现；
- 未增加参数类型校验（例如 `arguments` 不是 dict 时的友好错误），调用方 `_call_tool`
  已保证传入 dict；如未来出现外部调用者，可在 `call` 入口补一条 `ValidationError`；
- 本次未改动 `data/orders.json` 等演示数据；端到端验证只走读工具。

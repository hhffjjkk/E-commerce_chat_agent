# 阶段三 · 步骤 1 实现说明：Harness Tool Registry 与声明式注册

> 把 `LLMClient.TOOLS` 中硬编码的 6 条 schema 与 `_call_tool` 的名称→实现映射，
> 收敛到 `agent_system/harness/` 的 Tool Registry。工具行为、schema 内容、API 响应结构均未改变。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 新增子包 | `agent_system/harness/`（`tool_registry.py` / `builtin_tools.py` / `__init__.py`） |
| schema 来源 | `agent_system/tools.py::TOOL_SCHEMAS`（6 条，逐字迁移） |
| 对外入口 | `get_registry()`、`register_all()`、`ToolRegistry.schemas()` / `call()` |
| 注册时机 | `app.py` 创建 FastAPI 前；`demo.py` 与 `tests/conftest.py` 导入时；`register_all()` 幂等 |
| schema 保真 | SHA-256 与迁移前完全一致：`4c46bb1e…f9344e` |
| 测试 | 57 passed → **73 passed**（新增 16 项） |
| `.gitignore` | 已移除 `test_*.py` 规则 |

## 2. 设计动机

重构前存在两份需要人工同步的元数据：

1. `LLMClient.TOOLS`：硬编码 6 条 OpenAI JSON Schema；
2. `LLMClient._call_tool`：`name -> tools.*` 的字典映射。

问题：新增/调整工具时必须同时改两处；Mock 路径又直接调用 `tools.*`，无法统一启停。
Registry 把「声明（schema + 实现 + 标签 + 启停）」收敛到一处，调用方只依赖 `schemas()` / `call()`，
为阶段四（按 Agent 分配工具子集）和后续运行时策略留出接口。

## 3. schema 迁移对照

| 工具名 | 迁移前位置 | 迁移后位置 | 调用实现 |
| --- | --- | --- | --- |
| `query_product` | `agent.py` `LLMClient.TOOLS[0]`（原 L188–L202） | `tools.TOOL_SCHEMAS["query_product"]` | `tools.query_product` |
| `query_order` | `LLMClient.TOOLS[1]`（原 L203–L216） | `TOOL_SCHEMAS["query_order"]` | `tools.query_order` |
| `query_logistics` | `LLMClient.TOOLS[2]`（原 L217–L230） | `TOOL_SCHEMAS["query_logistics"]` | `tools.query_logistics` |
| `search_faq` | `LLMClient.TOOLS[3]`（原 L231–L244） | `TOOL_SCHEMAS["search_faq"]` | `tools.search_faq` |
| `apply_refund` | `LLMClient.TOOLS[4]`（原 L245–L259） | `TOOL_SCHEMAS["apply_refund"]` | `tools.apply_refund` |
| `cancel_order` | `LLMClient.TOOLS[5]`（原 L260–L274） | `TOOL_SCHEMAS["cancel_order"]` | `tools.cancel_order` |

`TOOL_SCHEMAS` 每项只保存 `description` 与 `parameters`（`name` 由字典 key 提供），
字段、类型、`required`、文案与迁移前逐字一致。

### 3.1 保真验证方法

迁移前从旧 `LLMClient.TOOLS` 计算快照：

```text
sha256(json.dumps(TOOLS, sort_keys=True, ensure_ascii=False)) =
4c46bb1ee6695d376e5821bb29ac28c5f8221964cbbaab7a6c1bfbb1eef9344e
```

`tests/test_builtin_tools_migration.py` 对 `get_registry().schemas()` 重新计算同一哈希并断言相等；
同时校验 6 个名称及顺序。任何 description / parameters / required 改动都会使测试失败。
（原 vendor 目录已删除，无法引用文件行号级 diff，故用哈希快照 + 人工清单双重锁定。）

## 4. 核心 API 与启停语义

### 4.1 `ToolSpec`

冻结数据类：`name` / `description` / `parameters` / `callable` / `enabled` / `tags` / `source`。
`callable` 不允许为 `None`；`name` 需匹配 `^[a-zA-Z_][a-zA-Z0-9_]*$`；
`parameters` 必须是 `type=object` 且含 `properties`，否则注册时抛中文 `ValueError`。

### 4.2 `ToolRegistry`

| 方法 | 行为 |
| --- | --- |
| `register(spec, override=False)` | 冲突默认 `ValueError`；`override=True` 覆盖并输出 `tool_registry_override` WARNING |
| `unregister(name)` | 不存在时静默返回 |
| `enable` / `disable` | 不存在时 `KeyError`；通过 `ToolSpec` 重建保持 frozen |
| `get(name)` | 不区分 enabled，未注册返回 `None` |
| `list(enabled_only, tags)` | tags 非空时“至少命中一个标签” |
| `schemas(names=None)` | 仅含 enabled；`names` 按给定顺序；未注册/禁用 → `ValueError`；`parameters` 深拷贝 |
| `call(name, **kwargs)` | 不存在/禁用 → `ToolExecutionError`（code=`tool_failed`，details 带 `tool`/`args_keys`） |
| `snapshot()` | 返回新 dict，外部增删不影响内部状态 |

启停效果：

- 禁用后 `schemas()` 不再下发该工具，模型不会再选择它；
- 若仍被调用（如 Mock 规则命中），`call()` 抛 `ToolExecutionError`，走步骤 2 的统一 500 `tool_failed`；
- `enable()` 后立即恢复，无需重启。

### 4.3 降级路径

Registry 自身不吞异常：工具业务失败仍由 `tools._tool_guard` 把数据/HTTP 异常转换为
`ToolExecutionError` / `DataSourceError`；Registry 只负责“工具不存在/已禁用”这一类分发失败。
`tool_call` 日志仍然只由 `_tool_guard` 记录一次，Registry 不重复埋点。

## 5. 注册时机与幂等

- `agent_system/app.py`：在 `app = FastAPI(...)` **之前**调用 `register_all()`；
- `agent_system/demo.py`：模块导入时调用，保证命令行独立运行；
- `tests/conftest.py`：导入时调用，保证只测 `harness` 的用例也有内置工具；
- 幂等实现：若同名 `ToolSpec` 完全相等则跳过；不等则抛
  `ValueError("builtin 工具定义被并发修改: ...")`，避免静默覆盖。

## 6. `TOOLS` 属性兼容处理

- `LLMClient.TOOLS` 由类属性改为 `@property`，返回 `get_registry().schemas()`；
- 全局检索确认项目内只有 `self.TOOLS`（`_run_tool_loop`）这一处调用，无 `LLMClient.TOOLS` 类级访问；
- 副作用：`LLMClient.TOOLS` 在类上访问得到 property 对象而非 list；项目内无此用法；
- `LLMClient()` 构造需要 API Key，验收命令若在 Mock 环境直接实例化会报 Key 缺失；
  测试使用 `object.__new__(LLMClient)` 验证，另已用伪造 Key 实例化验证 `TOOLS` 为 6 条。

## 7. Mock 路径

为保证“禁用工具后 Mock 模式同样生效”，`MockLLM.reply()` 中对 `query_order` /
`query_logistics` / `query_product` 的调用改为 `get_registry().call(...)`；
`list_product_keywords` 仍是内部辅助（不进 schema），保持直接调用。
工具启用时行为与重构前完全一致（底层仍是 `tools.*` + `_tool_guard`）。

## 8. tags 预埋（阶段四用途）

| 工具 | tags |
| --- | --- |
| `query_product` | `builtin`, `product` |
| `query_order` / `query_logistics` | `builtin`, `order` |
| `search_faq` | `builtin`, `faq` |
| `apply_refund` / `cancel_order` | `builtin`, `after_sales` |

阶段四可用 `registry.list(tags=("order",))` 或 `registry.schemas(names=[...])` 给不同 Agent
分配工具子集；本步骤不消费这两个过滤能力。

## 9. `.gitignore` 修复

- 删除 `test_*.py` 规则，`tests/test_*.py` 不再被忽略；
- 保留 `_*.py`（当前项目没有匹配文件）与 `*.db`（有意忽略 `sessions.db` 等运行期数据）；
- `refactor-notes.md` 的“遗留”第 1 条已更新为“已修复”。

## 10. 测试

```text
pytest tests/ -q
73 passed in 0.80s
```

新增 16 项：

- `tests/test_tool_registry.py`（11）：注册/查询、冲突与覆盖、启停与异常码、名称过滤、
  标签过滤、非法 spec、snapshot 浅拷贝、schemas 深拷贝、内置注册幂等、单例重置；
- `tests/test_builtin_tools_migration.py`（5）：schema 哈希保真、`LLMClient.TOOLS` 一致性、
  `_call_tool` 返回 JSON、禁用工具后 `/chat` 返回 `tool_failed`、非法 JSON 不泄露原文。

回归：既有 57 项全部通过（未修改断言）。

## 11. 验证记录

| 检查 | 结果 |
| --- | --- |
| `register_all(); len(get_registry().schemas())` | `6` |
| `LLMClient().TOOLS`（伪造 Key 实例化） | 6 条，名称顺序与迁移前一致 |
| `POST /chat` | 200，回复正常 |
| `GET /health` / `/health/ready` | 200 |
| `GET /chat/stream/schema` | 200 |
| `POST /chat/stream` | start → delta → `{"done":true}` → 扩展 done，顺序不变 |
| 禁用 `query_order` 后 `/chat` | 500 `tool_failed`，`details.tool=query_order` |
| `.gitignore` 检索 | 不再包含 `test_*.py` |

## 12. 遗留问题

1. **`TOOLS` 为实例属性**：类级 `LLMClient.TOOLS` 不再是 list；当前无类级调用，若后续需要需另设 `classproperty`。
2. **`LLMClient()` 依赖 API Key**：验收时需伪造 Key 或使用 `object.__new__`，属既有构造约束。
3. **Registry 进程内单例**：多进程部署时各进程独立，修改启停不会跨进程同步；当前无动态配置需求。
4. **tags 仅预埋**：尚未接入多 Agent 工具子集消费。
5. **`schemas(names=...)` 未接调用方**：阶段四路由/子 Agent 再消费。
6. **未提供运行时启停 API**：本步骤只提供 Python 接口，未暴露管理端点。

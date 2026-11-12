# 重构说明：代码归位与去包装（一步到位）

> 将阶段二交付的 `src/` 包装层与 vendor 业务代码合并为单一 `agent_system/` 包，
> 删除全部 monkey-patch / 运行期替换 / `sys.path` 注入 / `route.endpoint` 替换。
> 本次为纯结构重构：不新增功能、不改协议、不改 API 响应结构。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 目录 | `vendor/` 与 `src/` 已删除，业务与平台代码统一在 `agent_system/` |
| 入口 | `uvicorn agent_system.app:app` |
| 包装层 | `src/main.py`、`src/errors/wrappers.py`、`src/stream/middleware_bridge.py` 已删除并内联 |
| 测试 | 重构前 **57 passed** → 重构后 **57 passed**（无 skip、未放宽断言） |
| 路由行为 | `/chat`、`/chat/stream`、`/chat/stream/schema`、`/health`、`/health/ready`、`/webhook/{platform}`、`/` 实测正常 |
| vendor 依赖 | 合并进根目录 `requirements.txt`，新增 `langfuse>=3.0,<4.0` |

## 2. 迁移前后目录对照

| 重构前 | 重构后 |
| --- | --- |
| `vendor/ecommerce-customer-service-agent/agent.py` | `agent_system/agent.py` |
| `vendor/.../app.py` + `src/main.py` | `agent_system/app.py`（装配逻辑内联） |
| `vendor/.../{channels,datasource,tools,memory}.py` | `agent_system/` 同名文件 |
| `vendor/.../{demo,import_data}.py` | `agent_system/` 同名文件（import / 数据目录调整） |
| `src/config.py` | `agent_system/config.py`（移除 `ensure_vendor_path`） |
| `src/obs/*` | `agent_system/obs/*` |
| `src/errors/{codes,exceptions,handlers,fallback}.py` | `agent_system/errors/` 同名文件 |
| `src/errors/wrappers.py` | 已删除，逻辑内联到 `agent.py` / `tools.py` |
| `src/stream/{schema,emitter}.py` | `agent_system/stream/` 同名文件 |
| `src/stream/middleware_bridge.py` | 已删除，逻辑内联到 `app.py` 的 `/chat/stream` |
| `src/runtime/*` | `agent_system/runtime/*` |
| `src/routes/stream_schema.py` | `agent_system/runtime/stream_schema_route.py` |
| `src/eval/*` | `agent_system/eval/*` |
| `vendor/.../data/` | `data/` |
| `vendor/.../static/` | `static/` |
| `vendor/.../deploy/nginx.conf` | `deploy/nginx.conf`（`deploy/langfuse/` 保留） |
| `vendor/.../{Dockerfile,docker-compose.yml,README.md,.env.example,.gitignore,.dockerignore,requirements.txt}` | 根目录同名文件 |
| `vendor/.../.venv` | `.venv`（整体移动到根目录，已实测可用） |
| `vendor/.../{run.bat,run_web.bat}` | 根目录（命令同步更新） |

统一导入约定：跨包一律 `from agent_system.<子包>.<模块> import ...`；同包内保持原写法。

## 3. 删除清单与理由

| 删除项 | 理由 | 逻辑去向 |
| --- | --- | --- |
| `src/main.py` | 包装层 | 并入 `agent_system/app.py` |
| `src/errors/wrappers.py` | monkey-patch / 运行期属性替换 | `tools.py` 的 `_tool_guard` + `agent.py` 的 `_call_llm` |
| `src/stream/middleware_bridge.py` | `route.endpoint` 替换 | `app.py` 内联 `StreamEmitter` |
| `src/` 其余文件 | 已迁移 | `agent_system/` |
| `vendor/` 整目录（含 `.git` / `.env` / `sessions.db`） | 内容上提 | 根目录 / `agent_system/` |
| `requirements-obs.txt` | 依赖合并 | 根目录 `requirements.txt` |

说明：
- `vendor/.env` 未复制，避免把本地密钥带入新结构；模板统一由根目录 `.env.example` 提供；
- `vendor/sessions.db` 属于运行期数据，未迁移；
- `vendor/.git` 随目录删除，根目录原本不是 git 仓库，因此**未保留 upstream remote**（见第 9 节建议）。

## 4. 三个关键文件的内联改动

### 4.1 `agent_system/agent.py`

1. 顶部改为绝对导入：`agent_system.errors.exceptions.LLMError`、`agent_system.obs.events.log_event`、
   `agent_system.tools.*`。
2. 新增模块私有函数 `_map_llm_exception(exc)`：
   - 超时类（`TimeoutError` / 类名含 `Timeout`）→ 可重试；
   - 有状态码时：429 / 5xx → 可重试，其余 4xx → 不可重试；
   - 无法识别状态码 → 可重试（交给阶段三 Retry 决策）。
3. 新增 `LLMClient._call_llm()`：一次调用同时完成
   `llm_call(start/end)` 埋点、`latency_ms` 统计与异常分类；
   `_run_tool_loop` / `reply` / `reply_stream` 均改为调用它。
4. `stream=True` 的 `latency_ms` 只覆盖“拿到流对象”的耗时，代码注释中显式说明
   （沿用 phase2-step-1 notes 第 10 节口径）。
5. 工具调用的 `tool_call` 埋点**只保留在 `tools.py`**：
   MockLLM 会绕过 `_call_tool` 直接调用工具，若两处都记会产生重复事件；
   在 `_call_tool` docstring 中写明了这一分工。

### 4.2 `agent_system/tools.py`

1. 新增 `_tool_guard(name)` 装饰器（中文 docstring + 行内注释），统一处理：
   - `AgentError` → 原样抛出（避免重复包装）；
   - `json.JSONDecodeError` / `KeyError` / `FileNotFoundError` → `ToolExecutionError`；
   - HTTP 类异常（httpx / requests，按可用性合并为 `_HTTP_ERRORS`）→ `DataSourceError`；
   - 其他异常 → `ToolExecutionError`；
   - `finally` 中记录一条 `tool_call`（成功失败都记，`error_type` 仅失败时输出）。
2. 7 个工具函数全部加装饰器（需求列了 6 个；`list_product_keywords` 也纳入，
   因为重构前全局包装覆盖了它，Mock 路径会产生该工具的 `tool_call` 事件，必须保持一致）。
3. `args_keys` 只记参数名，业务失败（`found=False`）仍是正常返回值，不抛异常。

### 4.3 `agent_system/app.py`

1. 应用构造前依次完成：`get_settings()` → `configure_logging()` → `config_loaded` →
   `init_langfuse()` → `register_hook(NoOpFallbackHook())`。
2. 保留 `_disable_native_telemetry()`：关闭 FastAPI 0.162+ 内置 telemetry，
   避免与 Langfuse 映射重复产生 trace。
3. `/chat/stream` 内联 `StreamEmitter`：
   - 端点入口读取 `request_id` / `trace_id` 并传入 emitter；
   - `_original_stream_logic()` 逐字保留原 `generate()` 业务逻辑；
   - 外层仍用 `wrap_streaming_generator` 注入 `start` / `error` / 扩展 `done`，
     既有 `{"delta": ...}`、`{"done": true}` 不变，且只有一个 `event=done`。
4. 注册顺序不变：异常处理器 / 健康检查 / 协议自查 → CORS → BodyLimit → RateLimit →
   RequestID（`add_middleware` 逆序，实际执行为 request_id → rate_limit → body_limit → cors）。
5. `BASE_DIR` 改为项目根目录，静态页与 `data/` 都从根目录解析。
6. `/chat`、`/webhook/{platform}`、`/` 业务逻辑未改。

## 5. `log_event → langfuse_mapping` 内部转发

- `agent_system/obs/events.py` 的 `log_event` 在写完 stdout JSON 后，延迟导入并调用
  `langfuse_mapping.handle_event(event, payload)`；
- 业务代码（agent / tools / app）只调用 `log_event`，对 Langfuse 无感知；
- 未启用 Langfuse（无密钥 / 鉴权失败）时映射层立即返回，不再有外部 wrap 或额外线程；
- 转发本身包在 try/except 中，观测失败不影响请求；
- 这样观测层仍是可替换实现：替换 `langfuse_client` / `langfuse_mapping` 即可，
  不影响日志与协议层。

## 6. 测试迁移

- 所有 `from src.*` 改为 `from agent_system.*`；
- `from src.main import app` → `from agent_system.app import app`；
- 测试内裸导入 `import app` / `import tools` / `from memory import ...` 改为
  `from agent_system import app` / `from agent_system import tools` / `from agent_system.memory import ...`；
- `tests/test_errors.py` 的 LLM 失败用例不再依赖 `instrument_llm`，改为
  `_FakeBot(LLMClient)`，直接走真实 `_call_llm` 的埋点与异常映射；
- `tests/test_request_id.py` 的 `llm_call` 用例改为直接调用 `LLMClient._call_llm`；
- 其余断言原样保留，未降低强度、未新增 skip。

结果：**重构前 57 passed → 重构后 57 passed**。

## 7. 配置、依赖与启动

- `requirements.txt`：原 6 项依赖 + `langfuse>=3.0,<4.0`，无版本锁定（与原策略一致）；
  差异仅为新增 1 行，其余未动；
- `.env.example`：追加 `SERVICE_NAME` / `LOG_LEVEL` / `LOG_FORMAT` / `CORS_ALLOW_ORIGINS` /
  `RATE_LIMIT_*` / `MAX_BODY_BYTES` / `LANGFUSE_*`，每项带默认值与用途注释；
- `Dockerfile`：`CMD` 改为 `uvicorn agent_system.app:app ... --workers 2`；
- `docker-compose.yml`：`app.command` 同步改为 `agent_system.app:app`，Redis 服务与端口未动；
- `scripts/dev_run.sh`：解释器路径改为 `.venv/Scripts/python.exe`，模块改为 `agent_system.app:app`；
- `run.bat` / `run_web.bat`：分别改为 `python -m agent_system.demo` 与
  `uvicorn agent_system.app:app`；
- `README.md`：更新启动命令与数据导入命令，并补充“重组为 `agent_system/` 包”的说明。

## 8. 验证记录

| 检查 | 结果 |
| --- | --- |
| `python -c "import agent_system.app"` | 通过，无 ImportError |
| `uvicorn agent_system.app:app` | 启动成功，日志含 `langfuse_init(enabled=false)` |
| `POST /chat` | 200，响应头 `x-request-id`，`reply` 正常 |
| `GET /health` | 200，含 `status/service/request_id` |
| `GET /health/ready` | 200，`redis=not_configured`，`data=4 files readable` |
| `GET /chat/stream/schema` | 200，`protocol_version=1`，8 类事件 |
| `POST /webhook/douyin` | 200，返回 `session_id` + `reply` |
| `POST /chat/stream` | `start → delta → {"done":true} → 扩展 done`，done 事件仅 1 个，无 trace 字段（no-op） |
| `python -m agent_system.eval.runner` | 输出 `tasks=3, tool_success_rate=1.0, error_rate=0.0` |
| 工具异常链路 | 测试直接 monkeypatch 数据源方法，仍返回 500 `tool_failed`（不再依赖包装层） |

## 9. git 与 upstream 处理

- 重构前项目根目录不是 git 仓库，只有 `vendor/` 内带 `.git`；本次按需求删除 `vendor/`，
  历史随目录一起移除；
- 未保留 `upstream` remote（根目录无仓库可挂）。如需对照上游，可执行：

```bash
git init
git add .
git commit -m "refactor: merge src wrapper into agent_system package"
git remote add upstream https://github.com/li-lu-z/ecommerce-customer-service-agent.git
git fetch upstream
```

- 后续与上游同步建议用 `git diff upstream/main -- agent_system/agent.py` 做人工对照，
  不再保留双份源码目录。

## 10. 遗留问题

1. **`.gitignore` 曾忽略 `test_*.py`**：已在阶段三步骤 1 修复（移除该规则），`tests/` 可正常入库；
   仍保留 `_*.py`（当前无匹配文件）与 `*.db`（有意忽略运行期会话数据）。
2. **README 的“目录结构”章节仍是旧扁平结构**：按需求只更新运行方式，未重写整篇文档。
3. **根目录不是 git 仓库**：历史与分支对比能力缺失，需按第 9 节初始化。
4. **流式 Langfuse 限制不变**：流内工具/LLM 事件仍不建子 span，只有根 span 与 metadata。
5. **Langfuse 真实 UI 端到端验证仍未执行**：本机未启动自托管实例、无真实密钥。
6. **Docker 镜像未在本机构建验证**：仅更新了 `CMD` 与 compose `command`。
7. **`run.bat` / `run_web.bat` / `.dockerignore` 为上游附带文件**：已同步命令，未新增其他用途。
8. **`sessions.db` 会在导入 `agent_system.app` 时自动创建于根目录**：属原 `create_store()`
   行为，已被 `.gitignore` 覆盖。

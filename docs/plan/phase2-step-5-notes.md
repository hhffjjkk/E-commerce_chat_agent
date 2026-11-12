# 阶段二 · 步骤 5 实现说明：Langfuse 观测与评估骨架接入

> 在步骤 1–4 的 JSON 日志与 NDJSON 协议之上，把 Langfuse 作为观测/评估层的**默认实现之一**接入：
> 初始化客户端（缺配置自动 no-op）、把已有事件映射为 trace/span/generation、在流式协议回填 `trace_id`，
> 并提供最小 code evaluator 与本地 runner。不引入 `@observe`、不修改业务函数、不接 LLM-as-Judge。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 依赖 | `langfuse>=3.0,<4.0`（实际安装 3.15.0），写入根目录 `requirements-obs.txt`，**未改 vendor 依赖** |
| vendor 是否被改动 | **否**（`git status` 为空） |
| 未配置密钥 | 完全 no-op：不导入 SDK 网络调用、NDJSON 无 `trace_id`/`span_id`、`/health/ready` 不新增检查项 |
| 配置但不可达 | 启动 `auth_check()` 失败 → 记录 `langfuse_auth_failed`，降级 no-op，服务正常启动 |
| 映射 | 事件驱动：根 span / tool 子 span / llm generation / error 状态 / rate_limited metadata |
| 测试 | `pytest tests/` → **57 passed**（既有 40 项 + 本步骤 17 项） |

## 2. no-op 策略

`src/obs/langfuse_client.py`：

- `is_enabled()` 仅当 `LANGFUSE_ENABLED=true` 且 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 均非空时为真；
- 未启用时 `get_langfuse()` 返回 `NoOpLangfuse`（所有方法返回 `None`，未知方法由 `__getattr__` 兜底），
  因此映射层/评估层无需到处写 `if enabled`；
- 初始化流程：
  1. 未配置 → `langfuse_init(enabled=false, reason=not_configured)`；
  2. 已配置 → 延迟 `from langfuse import Langfuse` 构造客户端并 `auth_check()`；
  3. 鉴权失败（返回 False 或抛异常）→ 尽力 `shutdown()`，记录 `langfuse_auth_failed`，
     再记录 `langfuse_init(enabled=false, reason=auth_failed)`，服务继续启动；
- `langfuse_init` 只记录 `enabled` / `base_url` / `reason`，**从不记录密钥**；
- 未配置时不会执行任何 SDK 网络调用（测试通过 monkeypatch `_create_client` 断言其未被调用）。

实测（无密钥）：

```json
{"event":"langfuse_init","enabled":false,"reason":"not_configured"}
```

实测（伪造密钥 + 不可达地址 `http://127.0.0.1:6399`）：

```json
{"event":"langfuse_auth_failed","error_type":"ConnectError"}
{"event":"langfuse_init","enabled":false,"reason":"auth_failed","base_url":"http://127.0.0.1:6399"}
```

`/chat` 仍返回 200，NDJSON 不含 trace 字段。

## 3. 事件 → Langfuse 映射

实现：`src/obs/langfuse_mapping.py`。`log_event` 在写完 stdout JSON 后，额外调用
`langfuse_mapping.handle_event(event, payload)`；该转发独立 try/except，观测失败不影响业务。

| 日志事件 | Langfuse 映射 | 字段说明 |
| --- | --- | --- |
| `request_start` | 根 span `agent.request`（`as_type="span"`），`trace_context.trace_id` 由 `request_id` 派生 | metadata：`request_id` / `path` / `method` |
| `tool_call` | 子 span `tool.<tool>`（`span.start_observation` 自动挂到根 span） | metadata：`tool` / `ok` / `latency_ms` / `args_keys`（仅参数名） |
| `llm_call`（start/end） | 合并为一个 generation `llm.call`（`as_type="generation"`） | `model` 取 start/end 任一处；metadata：`stream` / `ok` / `latency_ms` |
| `request_error` / `error_raised` | 根 span `level="ERROR"` + `status_message=<code>` | 只写 code，不写 `cause`、异常文本或堆栈 |
| `request_end` | 根 span update（metadata 含 `status` / `latency_ms` / `tools` / `error_code`）后 `end()` | 结束并清理请求状态 |
| `rate_limited` | 根 span metadata（`rate_limited=true`），不单独建 span | 流式请求只记录在最终 metadata |

- `trace_id` 派生：`sha256(request_id)[:32]`，与 Langfuse SDK 的 `create_trace_id(seed=...)` 结果一致
  （已用 SDK 3.15.0 实测比对），保证不依赖 SDK 也能得到相同稳定 ID；
- 所有映射调用都包在 try/except 中，异常时清理状态，绝不影响请求；
- 映射不修改 `agent.py` / `tools.py`，也不使用 `@observe`。

## 4. 流式约束与已有限制

实现：`src/obs/langfuse_stream.py` + `StreamEmitter.trace_id_provider`。

- **不在** `/chat/stream` 端点或生成器上使用 `@observe`；
- 流式请求（`path == "/chat/stream"`）的映射策略：
  - `request_start` 创建**唯一**根 span；
  - `tool_call` / `llm_call` **不创建子 span**（只把工具名累加到根 span metadata），
    因为它们在生成器 `yield` 之后才发生，避免 Langfuse 上下文分裂；
  - `request_end` 时才 update + end 根 span；
- `StreamEmitter` 新增可选 `trace_id_provider`，仅在 `emit_start` / `emit_done` 前解析一次并缓存，
  因此一次流只有一条 trace，`start` 与 `done` 的 `trace_id` 严格一致；
- 未启用时 provider 返回 `None`，emitter 不输出该字段，行为与步骤 4 完全一致。

**已知限制（显式说明）**：

1. 流开始后的异常发生在 `yield` 之后，按约束**不再更新 Langfuse 根 span 的 error 状态**；
   错误只体现在 NDJSON 的 `error` 事件与 `done(status="failed")`，以及 `request_end` 的
   `metadata.error_code`。这是为了避免流式上下文丢失导致 trace 分裂。
2. 根 span 的 `end()` 在请求结束（`request_end`）时调用，晚于 NDJSON 的 `done` 事件，
   但使用的是手动持有的 span 对象，不依赖 `@observe` 上下文，因此不会分裂 trace。
3. 流式请求在 Langfuse 中只有根 span（+ metadata），没有逐工具的 tool span / generation；
   如需完整轨迹，应在阶段三引入 Harness 运行时后，在 yield 前完成子 span。

## 5. Code evaluator

实现：`src/eval/code_evaluators.py`（确定性，不调用 LLM、不访问网络）。

| 分数 | 逻辑 | 输出 |
| --- | --- | --- |
| `tool_success_rate` | `ok=true` 的 tool_call 数 / tool_call 总数；无工具调用返回 `None` | `{"name":"tool_success_rate","value":0-1,"data_type":"NUMERIC"}` |
| `error_rate` | 轨迹中出现 `error_raised` 或 `request_error` 记 1，否则 0 | `{"name":"error_rate","value":0/1,"data_type":"BOOLEAN"}` |

上报：`report_scores(client, trace_id, scores)` 优先使用 v3 的 `create_score`，兼容旧版 `score`；
NoOp 客户端下为无副作用调用。

## 6. 本地评估 runner

实现：`src/eval/runner.py`，任务集：`evals/tasks.jsonl`。

- 使用临时 SQLite 会话库（不写 vendor 数据目录），进程内 `httpx.ASGITransport` 调用 `/chat`；
- 抓取每次请求的 JSON 日志轨迹（按 `request_id` 过滤），调用 `evaluate_trajectory`；
- Langfuse 启用时把分数上报到 `trace_id_for(request_id)`；
- 输出汇总 JSON 到 stdout（任务数 / 平均工具成功率 / 错误率 / 逐任务明细）。

实测：

```json
{
  "tasks": 3,
  "tool_success_rate": 1.0,
  "error_rate": 0.0,
  ...
}
```

运行方式：`python -m src.eval.runner`（可加 `--tasks <path>`）。

## 7. 自托管 Langfuse（本地开发）

`deploy/langfuse/docker-compose.yml`：官方 v3 六件套（`langfuse-web` + `langfuse-worker` +
`postgres` + `clickhouse` + `redis` + `minio`），暴露 `3000`，与 vendor 的 compose 完全独立。

启动步骤：

```bash
docker compose -f deploy/langfuse/docker-compose.yml up -d
# 打开 http://localhost:3000 ，用 compose 中的 LANGFUSE_INIT_USER_EMAIL/PASSWORD 登录
# 在项目设置里创建 API Key（Public / Secret）
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...
export LANGFUSE_BASE_URL=http://localhost:3000
python -m uvicorn src.main:app
```

说明：该 compose 仅用于本地开发/测试，不追求高可用；`minio-init` 会用 `mc` 幂等创建
`langfuse` bucket（v3 要求事件桶预先存在，否则首批 span 上报会 500）。若 registry 拉取慢，
compose 中的镜像已固定到可用的国内镜像源（`docker.m.daocloud.io` / `docker.1panel.live`）。

## 8. Langfuse 作为可替换实现的接口边界

- 观测层入口：`log_event(...)` → `langfuse_mapping.handle_event(event, payload)`，
  业务代码只依赖日志事件，不依赖 Langfuse；
- 评估层入口：`evaluate_trajectory(events)` → `report_scores(client, trace_id, scores)`，
  分数结构与上报客户端解耦；
- 替换实现时只需替换 `langfuse_client` / `langfuse_mapping` 的内部实现，步骤 1–4 的日志与
  流式协议不变；阶段三 Harness 运行时沿用同一事件接口。

## 9. vendor 改动

无。`agent.py` / `tools.py` / `static/index.html` / vendor `requirements.txt` / vendor `docker-compose.yml`
均未修改；Langfuse 依赖写在根目录 `requirements-obs.txt`。

## 10. 测试

```text
pytest tests/ -q
57 passed in 0.74s
```

新增 17 项：

- `tests/test_langfuse_noop.py`（4）：no-op 客户端、初始化不触碰 SDK、`/chat` 与 `/chat/stream`
  无 trace 字段；
- `tests/test_langfuse_mapping.py`（7）：trace id 稳定性、根 span + tool 子 span、错误状态只含 code、
  llm generation、流式不创建子 span、trace 上下文复位、emitter provider 只解析一次；
- `tests/test_code_evaluator.py`（6）：成功率 2/3、无工具返回 None、错误率、轨迹合并、
  确定性（无 LLM）、`create_score` 上报。

回归：步骤 1–4 的 40 项全部通过。

## 11. 验证结果（启用 vs 未启用）

| 模式 | 验证方式 | 结果 |
| --- | --- | --- |
| 未启用 | 无密钥启动 + `/chat`、`/chat/stream` | 与步骤 4 一致；`langfuse_init(enabled=false)`；NDJSON 无 trace 字段 |
| 启用但不可达 | 伪造密钥 + `127.0.0.1:6399` | `auth_check` 失败 → `langfuse_auth_failed` → 降级 no-op；`/chat` 200 |
| 启用且可用 | 自托管 Langfuse 3.227.0（`deploy/langfuse/docker-compose.yml`）+ 固定本地 Key | 通过：`/chat` trace 含根 span + 工具 span；`/chat/stream` 仅一条 trace，NDJSON `start`/`done` 的 `trace_id` 与 Langfuse 实际 trace id 一致 |

### 11.1 端到端验证记录

环境：Langfuse `3.227.0`（self-host compose），`LANGFUSE_BASE_URL=http://localhost:3000`，
应用通过 `uvicorn src.main:app` 启动。

```text
CHAT 200
STREAM start_trace = a72c65c7165aec2ed926c47103129522
STREAM done_trace  = a72c65c7165aec2ed926c47103129522   (status=ok)
TRACE[chat]   id=90bb70ea... name=agent.request obs=[('tool.query_order','SPAN'),('agent.request','SPAN')]
TRACE[stream] id=a72c65c7... name=agent.request obs=[('agent.request','SPAN')]
```

- `/chat` 的非流式请求在 Langfuse 中可见根 span 与 `tool.query_order` 子 span；
- `/chat/stream` 只有一条 trace，`start` 与 `done` 的 `trace_id` 完全一致；
- 本地 runner 在启用模式下额外上报了 6 条 score（3 个任务 × `tool_success_rate` / `error_rate`），
  可通过 `GET /api/public/scores` 查询到。

### 11.2 验证期间发现并修复的问题

1. **FastAPI 内置 telemetry 造成重复 trace**：FastAPI 0.162 起默认启用内置 OpenTelemetry
   telemetry，会复用 Langfuse 设置的全局 TracerProvider，额外生成 `POST /chat` trace，
   导致一次请求在 UI 中出现两条 trace。已在 `src/main.py` 通过
   `_disable_native_telemetry(app)` 关闭 `tracing` / `metrics` / `logs` / `operation_spans`
   （FastAPI 构造后没有公开开关，只能调整其 `_telemetry` 字典；NativeTelemetry 持有同一引用）。
   修复后新请求只产生 `agent.request` 一条 trace（历史重复 trace 已通过 API 清理）。
2. **MinIO bucket 缺失导致首批上报 500**：Langfuse v3 要求 `langfuse` bucket 预先存在，
   compose 增加 `minio-init` 服务自动创建，避免 `Failed to export spans ... 500`。
3. **Docker Hub 直连过慢**：本地拉取 6 个镜像时约 2 MB/分钟，已改用镜像源
   （langfuse/postgres/clickhouse/redis 走 `docker.m.daocloud.io`，minio 走 `docker.1panel.live`）。

> 修复 1 属于本步骤运行期兼容处理，不改变事件协议与 vendor 代码。

## 12. 遗留问题

1. **流式 trace 不完整**：流内工具/LLM 事件不建子 span（见第 4 节限制），只有根 span 与 metadata。
2. **流内错误不更新根 span 状态**：仅在 `done(status="failed")` 与 `request_end` metadata 中体现。
3. **无日志采样/脱敏策略**：当前事件本身不含敏感值；若后续新增字段需补充 Langfuse mask 规则。
4. **runner 的 `expected_tool` 尚未参与评分**：本步骤只算成功率/错误率，工具命中率留待后续。
5. **未接入 Langfuse dataset / LLM-as-Judge**：属阶段六。
6. **`/health/ready` 不检查 Langfuse**：避免就绪探针依赖外部观测服务（按需求保持）。
7. **自托管 compose 为开发级**：未做高可用、备份、TLS 与正式密钥管理。

## 13. 复现命令

```bash
# 安装观测依赖（根目录，不影响 vendor）
pip install -r requirements-obs.txt

# 全部测试
vendor/ecommerce-customer-service-agent/.venv/Scripts/python.exe -m pytest tests/ -q

# 本地评估
python -m src.eval.runner

# 未启用 Langfuse：直接启动即可
bash scripts/dev_run.sh

# 启用 Langfuse：先起 self-host，再设置三个环境变量
docker compose -f deploy/langfuse/docker-compose.yml up -d
export LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... LANGFUSE_BASE_URL=http://localhost:3000
bash scripts/dev_run.sh
```

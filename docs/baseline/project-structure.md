# 基础项目结构与模块职责梳理

## 0. 基线信息

| 项目 | 值 |
| --- | --- |
| 仓库 | `https://github.com/li-lu-z/ecommerce-customer-service-agent` |
| 克隆路径 | `vendor/ecommerce-customer-service-agent` |
| 克隆方式 | 本机直连 GitHub 超时，改用镜像 `https://gh-proxy.com/https://github.com/...` |
| 提交 | `a7eaa37a60eff81696ad4a9e7baf74bd91c7610b`（2026-10-07 18:56:44 +0800） |
| 本地 Python | 3.12.7（项目 Dockerfile 使用 3.13） |
| 虚拟环境 | `vendor/ecommerce-customer-service-agent/.venv` |

> 说明：本文件只描述代码实际行为，不修改基础项目源码。

---

## 1. 目录树（到三级）

```text
vendor/ecommerce-customer-service-agent/
├── agent.py              # Agent 核心：系统提示词、Mock 规则后端、LLM Function Calling 客户端
├── app.py                # FastAPI 服务：对话、流式、平台 webhook、静态页
├── channels.py           # 渠道适配：把淘宝/抖音/微信回调归一化为内部消息
├── datasource.py         # 数据源抽象：DataSource / JsonDataSource / ApiDataSource
├── tools.py              # 工具层：业务函数薄封装，按环境变量选择数据源
├── memory.py             # 会话记忆：SessionStore(SQLite) / RedisSessionStore / create_store
├── demo.py               # 命令行多轮对话入口
├── import_data.py        # CSV -> data/*.json 导入脚本
├── requirements.txt      # 运行依赖（未锁版本）
├── README.md
├── Dockerfile
├── docker-compose.yml    # app + redis
├── .env.example          # 环境变量模板
├── .env                  # 本地新增，已被 .gitignore 忽略（仅占位，无真实密钥）
├── data/
│   ├── products.json     # 商品演示数据（12 条）
│   ├── orders.json       # 订单演示数据（10 条）
│   ├── logistics.json    # 物流轨迹演示数据（7 条）
│   └── faq.json          # FAQ 知识库（关键词检索）
├── deploy/
│   └── nginx.conf        # HTTPS 反向代理配置
├── static/
│   ├── index.html        # 单页聊天界面（流式渲染）
│   └── screenshot.png
└── .venv/                # 本地虚拟环境（已忽略）
```

---

## 2. 核心文件职责

| 文件 | 职责 | 关键位置 |
| --- | --- | --- |
| `agent.py` | 定义角色提示词与转人工规则；提供 `MockLLM`（本地规则）与 `LLMClient`（OpenAI 兼容接口）；内置 6 个函数调用 schema；实现最多 5 轮的工具循环与流式输出 | `SYSTEM_PROMPT` 22-29；`detect_escalation` 35-41；`MockLLM` 44-113；`LLMClient.TOOLS` 119-205；`_call_tool` 218-230；`_run_tool_loop` 240-258；`reply`/`reply_stream` 260-274 |
| `app.py` | FastAPI 应用；进程启动时按是否有 API Key 选择 LLM/Mock、按 `REDIS_URL` 选择记忆实现；暴露 `/chat`、`/chat/stream`、`/webhook/{platform}` 和静态页 | bot/store 初始化 23-28；`/chat` 42-50；`/chat/stream` 53-71；`/webhook` 74-94；`/` 97-99 |
| `tools.py` | 工具层薄封装，Agent 只依赖这些函数；模块导入时调用 `_get_source()` 决定使用 JSON 还是真实 API | `_get_source` 10-16；`_source` 19；6 个查询/写操作 22-48 |
| `datasource.py` | 数据源接口与两种实现；JSON 实现负责读写 `data/*.json`；API 实现是 HTTP 骨架 | `DataSource` 11-33；`JsonDataSource` 36-126；`ApiDataSource` 129-182 |
| `memory.py` | 会话历史持久化；SQLite 表 `messages`；Redis 使用 `chat:{session_id}` 列表；`create_store()` 按环境变量切换 | `SessionStore` 12-63；`RedisSessionStore` 66-93；`create_store` 96-100 |
| `channels.py` | 把不同平台回调字段映射为 `{session_id, message, user}` | `normalize` 6-38 |
| `demo.py` | 命令行循环，会话 ID 固定为 `"cli"`，复用同一 bot/store | 13-49 |
| `import_data.py` | 把店铺导出的 CSV 转为 JSON；数值字段转 int/float，多值字段按 `|` 拆分 | `SCHEMAS` 16-41，`convert` 44+ |
| `static/index.html` | 单页聊天 UI，`fetch('/chat/stream')` 读取 NDJSON，逐块追加文本；仅展示消息，不展示路由/耗时/缓存 | JS 323-350 |
| `Dockerfile` | `python:3.13-slim`，pip 安装依赖，uvicorn 2 worker 启动 | 3、18 |
| `docker-compose.yml` | app + redis:7-alpine；app 注入 `REDIS_URL=redis://redis:6379/0` | 1-24 |

---

## 3. 单 Agent 调用链路

### 3.1 HTTP 非流式 `/chat`

```text
POST /chat {session_id, message}
  -> app.chat (app.py:42)
  -> store.get(session_id)                        # 读取最近 20 条历史
  -> bot.reply(message, history=history)
       -> [Mock 模式] MockLLM.reply (agent.py:54)
            -> 正则识别快递单号 / 11 位订单号 / 商品关键词
            -> tools.query_* (tools.py:22-48)
            -> _source.query_* (datasource.py)
            -> JsonDataSource 读取 data/*.json
            -> 格式化中文回复
       -> [LLM 模式] LLMClient.reply (agent.py:260)
            -> _build_messages: system + history + user
            -> _run_tool_loop (最多 5 轮)
                 -> chat.completions.create(tools=6, tool_choice="auto")
                 -> msg.tool_calls -> _call_tool -> tools.* -> JSON 字符串
                 -> append {"role":"tool", ...} 后继续
            -> 再次 create() 生成最终回复（非流式）
  -> detect_escalation(message)                   # 命中敏感词或 >=3 个感叹号
  -> 追加转人工提示
  -> store.append(user) + store.append(assistant)
  -> ChatOut{reply}
```

### 3.2 流式 `/chat/stream`

工具循环阶段仍然是非流式；工具执行完后，最终回复用 `stream=True` 逐块产出。接口按 NDJSON 返回：

- 每行 `{"delta": "..."}`
- 最后一行 `{"done": true}`

历史在流结束后一次性写入（`app.py:67-68`）。

### 3.3 命令行 `demo.py`

同一套 `LLMClient/MockLLM + create_store()`，会话 ID 固定为 `cli`，支持多轮与逐字输出。

### 3.4 平台 webhook `/webhook/{platform}`

`channels.normalize()` 归一化字段后走与 `/chat` 相同的 bot/store 逻辑。签名校验尚未实现（`app.py:81` 为 TODO）。

---

## 4. 现有工具清单

工具 schema 定义在 `agent.py:119-205`，实现入口在 `tools.py`。

| 工具名 | 入参 | 返回结构 | 数据源 | 备注 |
| --- | --- | --- | --- | --- |
| `query_product` | `name: str` | `{found, name, keywords, price, stock, desc}` | `data/products.json` / `GET /products?name=` | JSON 实现用商品名 + keywords 做子串匹配，命中第一条 |
| `query_order` | `order_id: str`（11 位） | `{found, order_id, status, receiver, amount, logistics_company, tracking_no}` | `data/orders.json` / `GET /orders/{id}` | 精确匹配 |
| `query_logistics` | `tracking_no: str` | `{found, tracking_no, steps: list[str]}` | `data/logistics.json` / `GET /logistics/{id}` | 精确匹配 |
| `search_faq` | `question: str` | `{found, results: [{question, keywords, answer}]}` | `data/faq.json` / `GET /faq/search` | 关键词命中 +3 分，字符交集计分，取前 3 |
| `apply_refund` | `order_id: str, reason: str` | `{found, success, message}` | `data/orders.json` / `POST /orders/{id}/refund` | 写操作；`退款中`/`已取消` 拒绝重复申请 |
| `cancel_order` | `order_id: str` | `{found, success, message}` | `data/orders.json` / `POST /orders/{id}/cancel` | 写操作；仅 `待支付` 可取消 |

补充：

- `list_product_keywords()` 是 `MockLLM` 的内部辅助函数，不在 LLM 工具 schema 中。
- `ApiDataSource.list_product_keywords()` 直接返回 `[]`，因此 API 数据源模式下 `MockLLM` 的商品咨询无法命中；真实 LLM 模式不受影响。
- 工具异常没有统一捕获：`json.loads` 失败、数据源 HTTP 异常都会向上抛出，最终表现为 500。

---

## 5. 记忆存储现状

### 5.1 SQLite（默认）

- 实现：`SessionStore`（`memory.py:12-63`）。
- 位置：项目根目录 `sessions.db`（已被 `.gitignore` 的 `*.db` 忽略）。
- 表结构：

```sql
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
)
```

- 读取：`ORDER BY seq DESC LIMIT max_history`，再反转为正序；默认 `max_history=20` 条（不是 20 轮）。
- 写入：`seq = MAX(seq)+1`，随后删除超出最近 20 条的消息。
- 特点：单连接、`check_same_thread=False`、无锁、无时间戳、无 TTL；应用重启后历史仍在。
- 现状：只存 `user` / `assistant` 文本，不存工具调用轨迹、不存 summary。

### 5.2 Redis

- 实现：`RedisSessionStore`（`memory.py:66-93`）。
- Key：`chat:{session_id}`，类型为 list，元素是 `{"role": ..., "content": ...}` 的 JSON 字符串。
- 写入：`RPUSH` + `LTRIM -max_history -1`。
- 读取：`LRANGE 0 -1` 后取末尾 20 条。
- 特点：无 TTL、无租户前缀；Redis 不可用时没有降级回 SQLite 的逻辑。

### 5.3 切换方式

```python
# memory.py:96-100
def create_store(max_history: int = 20):
    if os.getenv("REDIS_URL"):
        return RedisSessionStore(max_history)
    return SessionStore(max_history)
```

- 设置 `REDIS_URL` -> Redis；不设置 -> SQLite。
- `app.py:28` 在模块导入时调用一次，因此必须在启动进程前设置环境变量，运行中无法切换。
- `docker-compose.yml` 会强制注入 `REDIS_URL`，容器部署默认走 Redis。
- 本地 `.env` 不会被 `app.py` / `demo.py` 自动加载（项目未调用 `load_dotenv`）；uvicorn 需显式 `--env-file .env`，`demo.py` 需在 shell 中预设环境变量。
- 本地已验证：Docker 容器 `baseline-redis`（`redis:7-alpine`，Server 7.4.11）可完成 `LLEN/LRANGE/LTRIM`，详见 `baseline-run.md` 第 4 节。

---

## 6. 数据源抽象现状

- 接口：`DataSource` 定义 7 个方法（`datasource.py:11-33`）。
- 演示实现：`JsonDataSource` 每次调用都读文件；退款/取消会直接改写 `data/orders.json`，重启后仍保留。
- 真实实现：`ApiDataSource` 使用 Bearer Token，`GET/POST` 超时 5 秒；响应必须是 dict，否则返回 `{"found": False}`。
- 选择逻辑在 `tools.py:10-19`，模块导入时执行一次：

```python
base_url = os.getenv("API_BASE_URL")
token = os.getenv("API_TOKEN")
if base_url and token:
    return ApiDataSource(base_url, token)
return JsonDataSource()
```

- 运行中不能切换数据源；`API_BASE_URL` 与 `API_TOKEN` 必须同时存在。
- JSON 文件是“读-改-写”模式，没有锁与事务，多请求并发写订单存在覆盖风险。

---

## 7. 与《总项目实现目标》描述的差异

| 目标文档中的“现状” | 代码实际 |
| --- | --- |
| Web 服务层缺失 | **已存在**：FastAPI 的 `/chat`、`/chat/stream`、`/webhook/{platform}`、静态页 |
| 前端界面缺失 | **已存在**：`static/index.html` 单页聊天界面，支持 NDJSON 流式渲染 |
| 只有 SQLite/Redis 记忆 | 属实，但 Redis 只存历史列表，无 TTL/会话状态缓存/语义缓存 |
| 已有工具层与数据源抽象 | 属实，工具 6 个，JSON/API 两种数据源 |
| 未提 Docker/Nginx | 实际已有 `Dockerfile`、`docker-compose.yml`、`deploy/nginx.conf` |

> 结论：阶段二“服务化补全”的目标需要调整——基础项目已有最小 Web 层和前端，后续应聚焦“流式协议增强、路由/耗时/缓存透出、鉴权与健康检查”，而不是从零补服务层。

---

## 8. 缺失项清单（后续改造依据）

### 8.1 多 Agent 与路由

- 无 Router Agent，无售前/订单/售后子 Agent，无工具子集隔离。
- 无路由结果、子 Agent 名称、工具轨迹的对外透出。

### 8.2 Harness 运行时

- **Tool Registry**：工具 schema 硬编码在 `agent.py:119-205`，无声明式注册、启停、按 Agent 分配。
- **Context Assembler**：仅 `system + 最近 20 条`，无 Token 预算、无稳定内容前置、无摘要压缩。
- **Loop Controller**：硬编码最多 5 轮；无超时、无中断、无最大工具调用次数配置。
- **Guardrails / Policy**：“先确认再执行”只写在提示词里，服务端没有确认状态机；工具层只校验订单状态。
- **Retry / Fallback**：LLM 与工具异常均无重试、无降级回复、无会话恢复。
- **Tracing / Eval**：无 request id、无工具耗时、无 LLM 调用计数、无评测接入点。

### 8.3 Web / 接口

- 无鉴权、限流、CORS 配置、健康检查、请求 ID、结构化日志。
- Webhook 验签是 TODO（`app.py:81`）。
- 只有 NDJSON 流式，无 WebSocket/SSE；流中不包含工具调用事件、路由信息、耗时。
- 无统一错误响应；工具/LLM 异常直接 500。

### 8.4 缓存

- Redis 仅作为历史消息列表，无 TTL、无多租户 Key 隔离、无会话状态缓存。
- 无语义缓存、无 FAQ/商品查询结果缓存、无命中率与节省调用量统计。

### 8.5 评测与压测

- 无任何测试（单元/集成/端到端），无固定任务集，无通过率/成本/P95 指标。
- 无压测脚本，无并发 QPS/错误率采集。

### 8.6 LLM 与配置

- 无 API 超时/重试/用量统计；无 token 计量与成本核算。
- `.env` 不会被应用自动加载；`.env.example` 中的占位 Key 若被加载会让 `LLMClient` 误判为“已配置”。
- `requirements.txt` 只有下限，无 lockfile；Docker 用 Python 3.13，本地验证用 3.12。
- 无 settings 模块，所有配置散落在 `os.getenv`。

### 8.7 数据与安全

- 任意会话可查询任意订单号，无用户身份绑定（隐私风险）。
- JSON 数据源读改写无锁/事务；演示数据会被真实退款/取消操作永久修改。
- 无输入长度限制、无敏感信息脱敏、无内容安全策略。

### 8.8 可观测性

- 无延迟直方图、工具失败率、LLM 调用量、缓存命中率等指标。
- 无 tracing，无法定位某次请求的 Agent 循环与工具执行细节。

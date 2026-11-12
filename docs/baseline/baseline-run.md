# 改造前单 Agent 跑通记录

## 0. 结论摘要

- 基础项目可以在本地安装依赖并启动，`GET /` 返回 200。
- 完成了普通问答、订单查询工具、物流查询工具、多轮会话、FAQ、售后意图、流式输出与转人工触发验证。
- **阻塞项 1**：本机没有可用的 `DEEPSEEK_API_KEY`，所有运行均使用项目内置 `MockLLM`（本地规则后端），未验证真实 LLM 的 Function Calling。
- **阻塞项 2（已解决）**：Docker 引擎此前是“启动未完成即探测”导致的误判；等待引擎就绪后已用 `redis:7-alpine` 容器跑通 Redis 记忆读写、TTL 与 20 条截断验证。
- 多轮对话验证结果：**SQLite 确实写入了历史，但 Mock 后端不读取历史**，指代消解请求（“那到哪了”）失败。真实 LLM 模式是否生效因缺少 Key 未验证。
- 未修改基础项目任何受版本控制的源码。

---

## 1. 环境与安装

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows（PowerShell 5） |
| Python | 3.12.7 |
| 虚拟环境 | `vendor/ecommerce-customer-service-agent/.venv` |
| pip | 26.3，镜像 `https://pypi.tuna.tsinghua.edu.cn/simple` |
| Git | 2.53.0.windows.3 |
| 基础项目提交 | `a7eaa37a60eff81696ad4a9e7baf74bd91c7610b` |
| LLM | 未配置 Key，实际为 `MockLLM`；预期真实模型 `deepseek-chat`（`LLM_BASE_URL=https://api.deepseek.com`） |

### 1.1 克隆

直连 GitHub 失败：

```text
fatal: unable to access 'https://github.com/li-lu-z/ecommerce-customer-service-agent.git/':
Failed to connect to github.com port 443 after 21109 ms: Could not connect to server
```

连通性探测结果：

```text
https://github.com      => 超时
https://gitee.com       => 200
https://gitclone.com    => 404
https://ghproxy.net     => 超时
https://gh-proxy.com    => 200
https://codeload.github.com => 超时
```

改用镜像克隆成功：

```powershell
git clone --depth 1 https://gh-proxy.com/https://github.com/li-lu-z/ecommerce-customer-service-agent.git vendor/ecommerce-customer-service-agent
```

### 1.2 安装依赖

```powershell
cd vendor/ecommerce-customer-service-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

安装成功，关键版本：

```text
fastapi==0.162.2
openai==3.46.0
pydantic==2.14.3
python-dotenv==1.2.4
redis==8.1.0
requests==2.34.2
starlette==1.10.0
uvicorn==0.54.0
```

### 1.3 密钥与 `.env`

- 本机 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 均未设置。
- 已创建 `vendor/ecommerce-customer-service-agent/.env`，只包含 `LLM_BASE_URL`、`LLM_MODEL` 和注释掉的 Key/Redis 配置，**没有任何真实密钥**。
- `.gitignore` 校验：

```text
.gitignore:9:.env            .env
.gitignore:13:*.db           sessions.db
```

- `git status --porcelain` 无输出，说明未改动受版本控制的文件。
- 重要：项目代码没有调用 `load_dotenv()`，`.env` 不会自动生效；uvicorn 需加 `--env-file .env`，`demo.py` 需要在 shell 中预设环境变量。
- 不要直接复制 `.env.example` 后保留 `DEEPSEEK_API_KEY=sk-你的key`：`app.py` 会据此误判为已配置并创建 `LLMClient`，随后请求真实 API 才失败。

---

## 2. 启动

```powershell
cd vendor/ecommerce-customer-service-agent
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8010 --env-file .env
```

验证：

```text
GET / => 200, bytes=11729
```

由于没有 Key，`app.py:23-27` 自动选择 `MockLLM`；`memory.create_store()` 在无 `REDIS_URL` 时选择 SQLite（`sessions.db`）。

---

## 3. 典型请求记录

所有请求均为 `POST http://127.0.0.1:8010/chat`，`Content-Type: application/json`，body 为 `{"session_id": "...", "message": "..."}`。

### 3.1 普通问答（S1）

- 输入：`你好，你能做什么？`
- 输出：

```text
抱歉，我暂时没理解您的问题。您可以这样问我：
· 商品：手机多少钱 / 耳机有货吗
· 订单：查订单 20261006001
· 物流：单号 SF123456789 到哪了
```

- 业务工具调用次数：0
- 观察：Mock 后端没有“自我介绍/能力说明”分支，普通问候被兜底回复；这不代表真实 LLM 的行为。

### 3.2 订单查询工具（S2）

- 输入：`查订单 20261101001`
- 输出：

```text
订单 20261101001 当前状态：已发货
收货人：林*，金额：¥5999
物流：顺丰（单号 SF2000000001）
```

- 触发工具：`query_order(order_id="20261101001")`
- 业务工具调用次数：1
- 数据源：`JsonDataSource` 读取 `data/orders.json`

### 3.3 物流查询工具（S3）

- 输入：`单号 SF2000000001 到哪了`
- 输出：

```text
快递单号 SF2000000001 物流轨迹：已揽收 → 运输中 → 到达上海分拨中心 → 派送中
```

- 触发工具：`query_logistics(tracking_no="SF2000000001")`
- 业务工具调用次数：1

### 3.4 商品查询（S4）

- 输入：`笔记本多少钱`
- 输出：

```text
极光笔记本电脑 14：14英寸2.8K屏 16GB+512GB 轻薄本
价格：¥5999
库存：有货（剩余 35 件）
```

- 触发函数：`list_product_keywords()` ×1 + `query_product("笔记本")` ×1
- 业务工具调用次数：1（`query_product`）；若把内部辅助函数也算入，共 2 次函数入口调用。

### 3.5 多轮对话与记忆（S5）

同一会话 `baseline-multiturn` 连续发送两条：

| 轮次 | 输入 | 输出 |
| --- | --- | --- |
| 1 | `查订单 20261101001` | `订单 20261101001 当前状态：已发货 ... 物流：顺丰（单号 SF2000000001）` |
| 2 | `那到哪了` | `抱歉，我暂时没理解您的问题。您可以这样问我：...` |

SQLite 中的实际记录（`sessions.db`）：

```text
seq=1  user       查订单 20261101001
seq=2  assistant  订单 20261101001 当前状态：已发货...
seq=3  user       那到哪了
seq=4  assistant  抱歉，我暂时没理解您的问题。...
```

结论：

- **记忆写入正常**：两条用户消息和两条助手回复都进了 SQLite。
- **上下文未生效**：`MockLLM.reply` 明确不使用 `history`（`agent.py:54-55` 注释“暂不使用历史上下文”），因此“那到哪了”无法继承上一轮订单号。
- 真实 LLM 路径会把 history 拼进 messages（`agent.py:232-238`），但由于没有 Key，本次未验证。

### 3.6 FAQ 政策问答（S6）

- 输入：`怎么退换货？`
- 输出：兜底回复（同 S1）
- 业务工具调用次数：0（仅内部 `list_product_keywords()` 1 次）
- 对照验证：直接调用 `JsonDataSource.search_faq("怎么退换货")` 能返回 3 条结果，首条为“怎么申请退换货？”。
- 结论：FAQ 检索能力存在，但 Mock 后端没有接入 `search_faq`；真实 LLM 模式由提示词要求调用该工具。

### 3.7 售后写操作（S7）

| 输入 | 输出 | 说明 |
| --- | --- | --- |
| `帮我取消订单 20261101002` | `订单 20261101002 当前状态：待支付 ...` | Mock 只做订单查询，没有调用 `cancel_order` |
| `订单 20261103001 我要退款` | `订单 20261103001 当前状态：退款中 ...` | 没有调用 `apply_refund` |

直接验证工具层服务端校验：

- `cancel_order("20261101001")` -> `{"found": true, "success": false, "message": "订单当前状态为「已发货」，无法取消"}`
- `apply_refund("20261103001", "测试原因")` -> `{"found": true, "success": false, "message": "订单当前状态为「退款中」，无法重复申请退款"}`

结论：写操作的状态校验存在，但 Mock 对话链路无法触达；真实 LLM 对话链路未验证。

### 3.8 转人工触发（S8）

| 输入 | 是否追加提示 |
| --- | --- |
| `我要投诉！你们的服务太差了` | 是（命中“投诉”） |
| `在吗!!!` | 是（3 个感叹号） |
| `在吗！` | 否（仅 1 个中文感叹号） |

追加文本：

```text
（检测到您可能需要人工协助，可联系在线人工客服进一步处理）
```

### 3.9 流式输出（S9）

- 接口：`POST /chat/stream`
- 输入：`查订单 20261101001`
- 协议：NDJSON，`{"delta": ...}` 多行，最后 `{"done": true}`
- 首块耗时：2.6 ms；总耗时：8.2 ms（Mock 一次性 yield 完整回复，因此 TTFT 与总耗时接近）
- 最终文本与 S2 一致

---

## 4. Redis 记忆切换验证（已完成）

### 4.1 连接问题的原因与处理

最初失败不是代码问题，而是环境问题：

1. 本机没有 Windows 原生 Redis。
2. Docker Desktop 首次启动需要较长时间，之前的探测在引擎尚未就绪时就执行，得到 `npipe:////./pipe/dockerDesktopLinuxEngine` 连接失败；引擎随后又因启动流程未完成而退出，造成“反复不可用”的假象。
3. 排障期间在 WSL Ubuntu 24.04 中安装过 Redis 7.0.15，容器内 `redis-cli ping` 成功；但 WSL 发行版在命令会话结束后会停止，Redis 进程随之退出，Windows 侧再次连接被拒绝，不适合作为持久开发环境。
4. 最终方案：启动 Docker Desktop 后轮询 `docker info`，确认引擎稳定（`ServerVersion 29.4.1`），再用容器运行 Redis。

### 4.2 最终可用环境

```powershell
# 启动 Docker Desktop 后，确认引擎就绪（可能需要 10-90 秒）
docker info --format '{{.ServerVersion}}'

# 启动 Redis（已带自动重启策略）
docker run -d --name baseline-redis --restart unless-stopped -p 6379:6379 redis:7-alpine

# 验证
docker ps --filter name=baseline-redis --format '{{.Names}} | {{.Status}} | {{.Ports}}'
```

实际结果：

```text
baseline-redis | Up | 0.0.0.0:6379->6379/tcp, [::]:6379->6379/tcp
Redis ServerVersion: 7.4.11
Windows Python redis ping: True
```

日常启停：

```powershell
docker start baseline-redis     # Docker Desktop 已运行时
docker stop baseline-redis
docker exec baseline-redis redis-cli ping
```

> 容器使用 `--restart unless-stopped`，Docker Desktop 启动后会自动拉起；若 Docker Desktop 本身未运行，Redis 仍不可用。

### 4.3 切换方式与验证结果

```powershell
$env:REDIS_URL='redis://localhost:6379/0'
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8011 --env-file .env
```

- `create_store()` 选中 `RedisSessionStore`。
- 同一会话 `redis-baseline` 连续两轮后：

```text
LLEN chat:redis-baseline = 4
TTL  chat:redis-baseline = -1          # 无过期时间
LRANGE 返回 4 条：user / assistant / user / assistant
SQLite 中该 session 的行数 = 0          # 证明请求确实走了 Redis，而非 SQLite
```

- 继续追加到 12 轮（24 条消息）后：

```text
LLEN chat:redis-baseline = 20
首条 = 第 3 轮 user 消息                # 旧消息已被 LTRIM 截断
```

- Redis 模式端到端 `/chat` 延迟：第一轮 15.7 ms，第二轮 5.4 ms（Mock 后端，n=2，仅作连通性参考）。
- 与 SQLite 模式一致，Mock 仍不读取 history，因此“那到哪了”依旧无法继承订单号；这不影响 Redis 存储验证。

### 4.4 注意事项

- Redis Key 没有 TTL，历史会一直保留；生产化时需要补 TTL 与租户前缀。
- `REDIS_URL` 必须在启动进程前设置；`app.py` 在导入时创建 store，运行中切换无效。
- 本地 `.env` 中的 `REDIS_URL` 仍保持注释，默认仍走 SQLite；需要 Redis 时显式设置环境变量，避免 Docker 未启动时误连。

---

## 5. 失败与异常场景（如实记录）

| 场景 | 现象 | 原因 | 影响 |
| --- | --- | --- | --- |
| 无 API Key | 全部请求走 Mock | 本机未配置 `DEEPSEEK_API_KEY` | 未验证真实 Function Calling、真实延迟与 token 成本 |
| 普通问候 | 返回兜底话术 | Mock 无问候分支 | 不能作为用户体验基线 |
| FAQ 提问 | 返回兜底话术 | Mock 未接入 `search_faq` | 政策问答在 Mock 下不可用 |
| 多轮指代 | “那到哪了” 无法解析 | Mock 不读取 history | 多轮上下文在 Mock 下失效 |
| 售后写操作 | 只返回订单状态 | Mock 未接入 `apply_refund` / `cancel_order` | 退款/取消对话链路未验证 |
| Redis（已解决） | 曾连接被拒绝 | Docker 引擎启动未完成；WSL 发行版停止导致 Redis 退出 | 等待引擎就绪后容器方案已验证 |
| 首次请求 | 约 74.6 ms | Python/模块冷启动、PowerShell 客户端开销 | 后续请求稳定在 7-9 ms |
| 数据源 API 模式 | 未验证 | 无真实业务 API | `ApiDataSource` 仅有骨架 |
| Webhook | 未验证 | 无平台回调环境，且无验签实现 | 平台接入不可用 |

---

## 6. 复现命令汇总

```powershell
# 安装
cd vendor/ecommerce-customer-service-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 启动（Mock 模式）
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8010 --env-file .env

# 普通问答
curl.exe -s -X POST http://127.0.0.1:8010/chat -H "Content-Type: application/json" -d "{\"session_id\":\"s1\",\"message\":\"你好，你能做什么？\"}"

# 工具调用
curl.exe -s -X POST http://127.0.0.1:8010/chat -H "Content-Type: application/json" -d "{\"session_id\":\"s2\",\"message\":\"查订单 20261101001\"}"

# 多轮（同一 session_id 连续调用）
curl.exe -s -X POST http://127.0.0.1:8010/chat -H "Content-Type: application/json" -d "{\"session_id\":\"s3\",\"message\":\"查订单 20261101001\"}"
curl.exe -s -X POST http://127.0.0.1:8010/chat -H "Content-Type: application/json" -d "{\"session_id\":\"s3\",\"message\":\"那到哪了\"}"
```

> Windows PowerShell 下 `curl.exe` 的中文 body 可能需要先写入 UTF-8 文件再 `--data-binary @file`；本次实测使用 Python `requests` 脚本完成，避免编码问题。

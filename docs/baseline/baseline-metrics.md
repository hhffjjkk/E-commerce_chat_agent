# 改造前基线指标

## 0. 指标有效性声明

本次基线在 **无 LLM API Key** 的条件下采集，实际运行的是项目内置 `MockLLM`（关键词/正则规则），因此：

- **不能**把本页延迟当作“真实 LLM 客服”的性能基线；
- 本页数据可用于衡量 **改造后 Harness/路由/缓存引入的本地开销**（同一 Mock 模式对比）；
- 真实 Function Calling 的端到端延迟、token 成本、工具调用准确率需配置 Key 后重新采集，方法见第 7 节。

| 项 | 值 |
| --- | --- |
| 采集时间 | 2026-11-09 |
| 项目提交 | `a7eaa37a60eff81696ad4a9e7baf74bd91c7610b` |
| Python | 3.12.7 |
| 运行后端 | `MockLLM`（无 Key） |
| 记忆后端 | SQLite（`sessions.db`）；Redis 另做连通性验证 |
| 服务 | uvicorn 0.54.0，单进程，`127.0.0.1:8010`（SQLite）/ `8011`（Redis） |
| 向量/语义缓存 | 无 |
| Redis | 已验证：Docker `redis:7-alpine`，Server 7.4.11 |

---

## 1. 测量口径

- 客户端：Python 3.12 + `requests`，`time.perf_counter()` 包住一次 `POST /chat`。
- 起点：发起 HTTP 请求前；终点：收到并解析完整 JSON 响应后。
- 包含：TCP 建连（每次 `requests.post` 新建连接）、请求/响应序列化、FastAPI 路由、会话存储读写（SQLite 或 Redis）、JSON 数据文件读取、Mock 规则处理。
- 排除：浏览器渲染、前端 JS 执行、网络公网 RTT（本机回环）。
- 服务已预热；每个用例连续执行 3 次，每次使用不同 `session_id`，避免历史长度变化影响。
- 样本量 n=3，**不足以计算 P95**；本页给出 min / mean / max 与中位数。
- LLM 调用次数为 0，token 消耗为 0，成本为 ¥0。

---

## 2. 端到端延迟（非流式 `/chat`）

单位：毫秒。

| 用例 | 请求 | Run1 | Run2 | Run3 | 中位数 | 均值 | 最小 | 最大 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 普通问答 | `你好，你能做什么？` | 9.4 | 26.4 | 7.6 | 9.4 | **14.5** | 7.6 | 26.4 |
| 订单工具 | `查订单 20261101001` | 7.8 | 7.5 | 7.9 | 7.8 | **7.7** | 7.5 | 7.9 |
| 物流工具 | `单号 SF2000000001 到哪了` | 30.1 | 8.6 | 8.1 | 8.6 | **15.6** | 8.1 | 30.1 |
| FAQ（Mock 兜底） | `怎么退换货？` | 28.5 | 7.7 | 8.3 | 8.3 | **14.8** | 7.7 | 28.5 |

补充：

- **冷启动首请求**：74.6 ms（服务启动后第一次 HTTP 调用，含 PowerShell 客户端与模块懒加载开销）。
- 预热后稳定区间约 **7.5-9.4 ms**；28-30 ms 的离群值出现在单次运行的首个请求，判断为 SQLite 页分配/客户端连接开销，未做进一步隔离。
- 同一用例的 Run1 普遍高于 Run2/Run3（例如物流 30.1 vs 8.6/8.1），与 `session_id` 首次写入、SQLite 页分配相关。

### 2.1 流式 `/chat/stream`

| 指标 | 值 | 说明 |
| --- | --- | --- |
| 首块耗时 TTFT | **2.6 ms** | 从请求发出到收到第一行 `{"delta": ...}` |
| 总耗时 | **8.2 ms** | 到收到 `{"done": true}` |
| 块数量 | 1 个 delta + 1 个 done | Mock 一次性 `yield` 完整文本，非真实逐 token |
| 最终文本 | 与 S2 一致 | |

> Mock 的 TTFT 没有参考价值；真实 LLM 的 TTFT 需配置 Key 后测量。

---

## 3. 工具与 LLM 调用次数

### 3.1 单请求工具调用

| 用例 | 业务工具调用 | 内部辅助函数 | 合计函数入口 | 明细 |
| --- | --- | --- | --- | --- |
| 普通问答 | 0 | 1 | 1 | `list_product_keywords()` |
| 订单查询 | 1 | 0 | 1 | `query_order("20261101001")` |
| 物流查询 | 1 | 0 | 1 | `query_logistics("SF2000000001")` |
| 商品查询 | 1 | 1 | 2 | `list_product_keywords()` + `query_product("笔记本")` |
| FAQ | 0 | 1 | 1 | `list_product_keywords()`；`search_faq` 未被调用 |
| 取消订单 | 1 | 0 | 1 | `query_order`；`cancel_order` 未被调用（代码路径推断） |
| 退款 | 1 | 0 | 1 | `query_order`；`apply_refund` 未被调用（代码路径推断） |
| 转人工 | 0 | 1 | 1 | `list_product_keywords()` + 敏感词检测 |

### 3.2 LLM 相关

| 指标 | 值 |
| --- | --- |
| LLM 后端 | `MockLLM` |
| LLM API 调用次数 | **0** |
| 注册的工具 schema 数 | **6**（`query_product`、`query_order`、`query_logistics`、`search_faq`、`apply_refund`、`cancel_order`） |
| Function Calling 循环轮数 | 不适用（Mock 不走循环） |
| 最大循环轮数（代码上限） | 5（`agent.py:242`） |
| 输入/输出 token | 不适用 |
| 单请求 LLM 成本 | ¥0 |

---

## 4. 记忆与存储

| 指标 | 值 |
| --- | --- |
| 记忆实现 | `SessionStore`（SQLite） |
| 数据库 | `vendor/ecommerce-customer-service-agent/sessions.db` |
| 基线采集后总行数 | 56 |
| 会话数 | 27 |
| 文件大小 | 20,480 bytes |
| 单会话历史上限 | 20 条消息（默认 `max_history=20`） |
| 多轮上下文生效 | **否**（Mock 不读取 history） |
| Redis 读写 | **已验证**：`LLEN`、`LRANGE`、`LTRIM` 均符合预期 |
| Redis 选中逻辑 | 已验证：设置 `REDIS_URL` 时 `create_store()` 返回 `RedisSessionStore` |
| Redis 连接 | 成功：Docker 容器 `baseline-redis`，`127.0.0.1:6379`，Server 7.4.11 |
| Redis Key | `chat:{session_id}`，list，无 TTL（`TTL = -1`） |
| Redis 两轮后长度 | 4 条（user/assistant × 2） |
| Redis 12 轮后长度 | 20 条（被 `LTRIM -20 -1` 截断） |
| Redis 会话写入 SQLite 行数 | 0（证明请求走 Redis） |
| Redis 模式端到端延迟 | 第一轮 15.7 ms，第二轮 5.4 ms（n=2，仅连通性参考） |

---

## 5. 失败率与异常

样本均为规则命中路径，未做并发与故障注入，以下为定性记录：

| 指标 | 值 | 说明 |
| --- | --- | --- |
| HTTP 5xx | 0 / 12 次采样 | 未触发工具/LLM 异常 |
| 工具调用失败 | 0 | 均为本地 JSON 命中 |
| 业务工具未命中 | 2 类 | FAQ、售后写操作在 Mock 下不可达 |
| 多轮上下文失败 | 1 / 1 | “那到哪了”未解析 |
| 转人工误判 | 0 | 敏感词与 3 连感叹号按预期触发 |
| Redis 连接失败 | 0 / 1 | 首次 10061 失败已定位为 Docker 引擎未就绪，重试后成功 |

---

## 6. 指标解读

1. 预热后 8 ms 左右是 **纯本地规则 + 文件 I/O** 的成本，可作为“非 LLM 固定开销”的近似上限（改造后同一 Mock 模式不应显著高于该量级）。
2. `MockLLM` 每个请求都会调用 `list_product_keywords()` 并读取 `products.json`，这是可优化点，但当前开销很小。
3. 延迟离群值主要出现在每个用例的第一次请求，建议后续压测先做 warm-up，并区分冷/热启动。
4. 由于 LLM 调用为 0，本基线无法回答“单任务成本”“token 下降比例”“LLM 调用量下降比例”等核心指标，必须在真实 Key 下补采。

---

## 7. 真实 LLM 基线补采方法（待执行）

配置 Key 后，用同一组请求重新采集：

```powershell
cd vendor/ecommerce-customer-service-agent

# 1. 仅本次会话设置（不要写入任何文件）
$env:DEEPSEEK_API_KEY='<你的 key>'
$env:LLM_MODEL='deepseek-chat'
$env:LLM_BASE_URL='https://api.deepseek.com'

# 2. 启动（不传 .env 也可，环境变量优先）
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8012

# 3. 用同一脚本采集 /chat 与 /chat/stream
```

需要补齐的指标：

| 指标 | 采集方式 |
| --- | --- |
| LLM 调用次数 | 在客户端记录响应中的工具轨迹，或在服务端加临时计数（不提交） |
| 输入/输出 token | 解析 API 响应 `usage` 字段 |
| 单任务成本 | token × 官方单价 |
| TTFT / 总延迟 | 流式逐块计时 |
| 工具调用成功率 | 记录工具返回 `found/success` 与异常 |
| 多轮上下文准确率 | 固定用例：订单号指代、物流单号指代 |
| 失败率 | 统计异常响应与重试次数 |

> 补采完成前，任何“改造后延迟下降 X%”“成本下降 Y%”的结论都缺乏真实 LLM 基线支撑。

---

## 8. 原始数据存放

本次采集使用临时脚本，位于系统临时目录，未写入仓库：

- `%TEMP%\baseline_probe.py`（HTTP 延迟与多轮）
- `%TEMP%\baseline_probe2.py`（工具调用计数、数据源行为）
- `%TEMP%\baseline_probe3.py`（转人工、售后意图）
- `%TEMP%\redis_probe.py`（Redis 记忆读写、TTL、截断）

仓库中仅保留本目录下三份文档；`sessions.db` 被 `.gitignore` 忽略。

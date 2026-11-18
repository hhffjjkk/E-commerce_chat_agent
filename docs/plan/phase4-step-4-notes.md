# 阶段四步骤 4：前端 route 展示与可观测性收尾

本步骤只改动前端 `static/index.html`，不修改任何后端代码、NDJSON 协议或 `/chat` 响应结构。
原始文件已备份为 `docs/baseline/static-index-original.html`，便于 diff 与面试演示原始 UI。

## 1. 改动前的结构基线（读代码记录）

### 1.1 DOM 结构

| 元素 | 选择器 / id | 说明 |
|---|---|---|
| 页面容器 | `.chat-container` | 固定宽度卡片，flex 纵向布局 |
| 头部 | `.chat-header` | 头像 + 标题 + 在线状态 |
| 消息列表 | `#messages`（`.chat-messages`） | 所有消息的挂载点，自动滚动到底部 |
| 快捷问题 | `.quick-questions button` | 点击后直接发送按钮文案 |
| 输入框 | `#input` | 回车触发发送 |
| 发送按钮 | `#send` | 发送期间 `disabled` |
| 单条消息行 | `.msg-row.user` / `.msg-row.assistant` | 内含 `.avatar`、`.body` |
| 消息体 | `.body` → `.bubble` + `.time` | 气泡 + 时间戳（原文件已有的唯一元信息区域） |

原文件**没有**任何路由、来源、trace 相关的元信息区域，因此新增标签不会与既有内容重复。

### 1.2 JS 关键函数

原 `<script>` 中只有以下函数（没有 `renderDelta` / `finalizeMessage` 这类命名）：

| 函数 | 签名 | 职责 |
|---|---|---|
| `now()` | `() => string` | 返回 `HH:MM` 时间文本 |
| `addMessage(role, text)` | `(string, string) => HTMLElement` | 创建 `.msg-row` + 头像 + `.bubble` + `.time`，返回 bubble 元素 |
| `addTyping()` | `() => HTMLElement` | 创建带三点动画的 assistant 占位行，返回整行 |
| `removeTyping(row)` | `(HTMLElement) => void` | 移除占位行 |
| `send(text)` | `async (string?) => void` | 发送消息、读取流、追加文本 |

事件绑定：`#send` click、`#input` keydown(Enter)、快捷问题按钮 click；脚本末尾追加一条欢迎消息。

### 1.3 流解析基线

`send()` 内 `fetch("/chat/stream")` 后：

1. `resp.body.getReader()` + `TextDecoder` 增量解码；
2. 用 `buffer.indexOf("\n")` 按行切分，逐行 `JSON.parse`；
3. 只处理 `obj.delta`：首次 delta 时移除 typing 占位并创建 assistant 消息，后续 `bubble.textContent += obj.delta`；
4. 无法解析的行静默忽略；
5. 流结束（reader 返回 `done`）后，若从未收到 delta，则显示“（未收到回复，请稍后再试）”；
6. `catch` 中处理网络错误；`finally` 恢复按钮并聚焦输入框。

### 1.4 既有事件处理

原文件**只识别 `delta`**：`{"event":"start"}`、`{"event":"route"}`、`{"event":"error"}`、`{"event":"done"}` 以及 `{"done":true}` 都被忽略，不影响原有渲染。
本次改造新增对 `route` 与 `error` 的识别，`delta` / 结束语义保持不变。

## 2. route 渲染规则与降级策略

### 2.1 统一形状

`normalizeRoute(route)` 把两个来源归一为同一形状：

```text
{ agent, display_name, strategy, confidence, reason }
```

降级规则：

- `display_name` 为空或缺失 → 用 `agent`；
- 两者都无 → 返回 `null`，**不渲染任何新增元素**；
- `confidence` 非有限数字 → 记为 `null`（不展示）；数值越界 → 夹到 `[0,1]`；
- `strategy` / `reason` 非字符串或缺失 → 空串（策略只影响展示，原因只影响 tooltip）；
- 未知 `strategy` → 原样展示（保持可观测性，不隐藏新策略）。

### 2.2 消息级标签

- 位置：assistant 消息 `.body` 内、`.time` 之前；
- 文案：`display_name` + 灰色小字 `策略 · 置信度`，例如 `订单与物流` + `规则 · 0.67`；
- 策略映射：`rule → 规则`、`llm → LLM`、`fallback → 兜底`、`pending_override → 待确认`，其他原样；
- 置信度保留两位小数；缺失时只展示策略；
- `title` 展示 reason 的中文映射（`rule_hit/router → 规则命中`、`llm_hit → LLM 判定`、`below_threshold → 低于阈值`、`no_llm → LLM 不可用`、`parse_error → 解析失败`、`llm_exception → LLM 异常`、`router_exception → 路由异常`、`no_profiles → 无可用子 Agent`、`profile_missing → Profile 缺失`、`invalid_default_profile → 默认 Profile 无效`、`pending_override → 有未确认操作`，其他原样）；
- 流内出现 `{"event":"error"}` 或连接中断时，标签仍然渲染，并在末尾追加 `⚠`（错误详情不写入标签）；
- 标签**不展示**用户消息原文、`request_id`、`trace_id`。

### 2.3 时序

`/chat/stream` 的 `route` 事件通常在首个 `delta` 之前到达。前端只把它记录到本轮 `currentRoute`，**消息结束后**（读到流结尾）再统一渲染标签：

- 避免“标签先出现、正文为空”的视觉抖动；
- 避免流中途失败留下孤立标签；
- 若整轮都没有 delta，仍先创建提示消息承载标签，保证路由信息可见。

`/chat` 非流式路径在响应返回后立即渲染。

## 3. 数据来源

| 路径 | 来源 | 读取方式 |
|---|---|---|
| `/chat/stream` | NDJSON `{"event":"route", ...}` | `extractRouteFromStream(obj)` 判断 `obj.event === "route"` 后归一化 |
| `/chat` | 响应体 `route` 字段 | `normalizeRoute(data.route)` |

前端通过 URL 参数 `?mode=chat` 切换到非流式 `/chat` 进行对照验证；默认仍使用 `/chat/stream`。
非流式路径**不**请求 `/chat/stream/schema`，也不调用其他端点。

## 4. 旧后端兼容

- `/chat` 响应无 `route` 字段 → `normalizeRoute(undefined)` 返回 `null` → 不创建标签，DOM 与改动前一致；
- `/chat/stream` 无 `route` 事件 → `currentRoute` 保持 `null` → 同样不渲染；
- 顶部状态栏显示灰色“未路由”，这是唯一新增的常驻元素（可通过关闭按钮隐藏，见第 5 节）；
- 原有 delta 追加、`done` 结束、typing 动画、错误提示逻辑均未改动。

**兼容性验证方法（二选一）**：

1. `git stash` 暂存前端改动，用当前后端跑一遍记录 DOM；恢复后临时注释 `agent_system/app.py` 中写入 `route` 的分支（或使用阶段四步骤 2 之前的镜像），刷新页面确认无标签；
2. 直接用一个返回 `{"reply": "..."}`（无 `route`）的 mock 服务替换 `fetch` 目标，确认页面行为与原版一致。

## 5. 顶部状态栏与调试面板的设计取舍

### 5.1 顶部状态栏

- 位于头部与消息列表之间，`role="status"` + `aria-live="polite"`，内容变化时由屏幕阅读器朗读；
- 无 route 时显示灰色“未路由”；
- 同一会话切换 Profile（如先 order 后 after_sales）显示 `订单与物流 → 售后服务`；
- 关闭按钮把 `localStorage.show_route_bar = "0"`；调试面板里的“显示状态栏”按钮恢复为 `"1"`，默认展示；
- **为什么不用后端配置**：展示偏好是纯前端状态，写入后端会引入无谓的配置项、接口与迁移成本；
- 关闭状态栏不影响消息级标签。

### 5.2 调试面板

- 右下角 `<button>` 折叠按钮，`aria-expanded` 随状态切换，展开时显示最近 20 条 route 原始 JSON（单行紧凑）；
- 默认折叠；`/chat/stream` 与 `/chat` 的 route 都进入同一队列；
- 这是开发辅助而非用户功能，生产环境可整体移除；本步骤保留以便演示与排查。

## 6. 可访问性

- 消息级标签带 `aria-label`：`路由：{display_name}，策略：{strategy}，置信度：{confidence}`，confidence 缺失时省略该分句；
- 顶部状态栏 `role="status" aria-live="polite"`；
- 调试折叠按钮是原生 `<button>`，带 `aria-expanded` / `aria-controls`，可键盘操作；
- 次要文字使用 `#595959`（白底满足 WCAG AA）；
- 策略通过文字表达，颜色只做辅助，色盲友好。

## 7. 手动验证清单

| 场景 | 操作 | 预期 |
|---|---|---|
| `/chat/stream` 正常路径 | 发“我的订单到哪了” | 流结束后消息旁显示 `订单与物流` + `规则 · 0.67`，tooltip 显示“规则命中” |
| `/chat/stream` 售后退款 | 发“我要退款” | 标签显示 `售后服务`；低于阈值走 LLM 时展示 `LLM · ...` |
| `/chat` 非流式 | 访问 `/?mode=chat` 后发“这个商品多少钱” | 消息旁显示 `售前咨询` + `规则 · 0.67` |
| pending 粘滞 | 先触发退款拿到 `needs_confirmation`，再发“确认” | 标签显示 `售后服务` + `待确认 · 1.00`，tooltip“有未确认操作” |
| 旧后端兼容 | 注释后端 route 分支或使用旧镜像 | 消息旁无标签，DOM 与改动前一致；顶部状态栏显示“未路由” |
| 流中错误 | fake LLM 抛错（或断网） | 错误消息仍带标签，标签末尾追加 `⚠` |
| 流中途断开 | 手动中断请求后再发一次 | 前端显示错误提示且不崩溃，下一次请求标签正常 |
| 调试面板 | 点击右下角折叠按钮 | 展示最近 20 条 route 原始 JSON；再次点击收起 |
| 可访问性 | Tab 遍历 + 屏幕阅读器（可选） | 折叠按钮可键盘操作；标签 aria-label 正确朗读 |
| 顶部状态栏切换 | 同一会话先 order 后 after_sales | 状态栏展示 `订单与物流 → 售后服务` |

## 8. 前端改动摘要

新增 DOM：

- `#route-bar`（含 `#route-bar-value`、`#route-bar-close`）；
- `.debug-widget`（`#debug-toggle`、`#debug-panel`、`#debug-list`、`#route-bar-restore`）。

新增 CSS 类：`.route-bar`、`.route-bar-label`、`.route-bar-value`、`.route-bar-value.muted`、`.route-bar-close`、`.route-badge`、`.route-meta`、`.route-error`、`.debug-widget`、`.debug-toggle`、`.debug-panel`、`.debug-header`、`.debug-list`、`.debug-line`、`.debug-empty`。

新增 JS：

| 函数 | 作用 |
|---|---|
| `normalizeRoute(route)` | 字段校验、`display_name → agent` 降级、confidence 夹取 |
| `extractRouteFromStream(obj)` | 从 NDJSON 事件提取 route，非 route 事件返回 `null` |
| `renderRouteBadge(route, hasError)` | 生成消息级标签（含 title / aria-label / ⚠） |
| `updateRouteBar(route)` | 更新顶部状态栏与 Profile 切换箭头 |
| `pushRouteDebug(rawRoute)` | 调试面板入队，最多 20 条 |

`send()` 的改动仅限：记录 `currentRoute` / `streamErrored`、新增 `?mode=chat` 分支、在消息结束后追加标签并更新状态栏。`addMessage` / `addTyping` / `removeTyping` 的签名与行为未变，delta 追加与结束判断逻辑不变。

## 9. 测试与遗留

### 9.1 已实际验证

- `pytest tests/`：**237 passed**（前端改动不涉及 Python）；
- 浏览器实测（本地 MockLLM，`127.0.0.1:8011`）：
  - `/chat/stream` 发“我的订单到哪了” → 标签 `订单与物流` + `规则 · 0.67`，顶部状态栏同步；
  - `/?mode=chat` 发“我的订单到哪了” → 标签与状态栏一致，证明 `/chat` 响应体 route 生效；
- 旧后端兼容：用临时 mock 服务只返回 `start/delta/done`（无 `route`）→ 消息旁无标签、顶部状态栏“未路由”、调试面板“暂无路由数据”，原有渲染正常。

### 9.2 遗留

- `static/screenshot.png` 未更新（展示变化后的截图列为遗留）；
- 未引入 Playwright / Cypress 等自动化 e2e：本步骤是纯展示增量，人工清单足够，且避免引入构建与浏览器依赖；
- 浏览器兼容性以现代 Chromium / Firefox / Safari 为准（使用 `fetch` + `ReadableStream` + `crypto.randomUUID`，与原文件一致）；
- **后端既有缺陷（非本步骤引入，未修复）**：`agent_system/agent.py` 中 `registry.call("query_product", name=key)` 与 `ToolRegistry.call(name, **kwargs)` 的参数名冲突，命中商品关键词（如“极光笔记本多少钱”）时抛 `TypeError: got multiple values for argument 'name'`，`/chat` 与 `/chat/stream` 均返回 `internal_error`。该问题属于阶段三 Tool Registry 迁移遗留，本步骤按要求不修改后端，建议下阶段单独修复（可把工具入参改名或让 Registry 支持显式 `arguments` 字典）。

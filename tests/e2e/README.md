# 前端 e2e（手动验证）

本目录不包含自动化测试。阶段四步骤 4 的前端 route 展示采用**人工验证**，原因：

- 改动只是展示层增量，核心断言（route 字段、事件顺序）已由后端 pytest 覆盖；
- 不引入 Playwright / Cypress，避免为一次展示改动新增浏览器与构建依赖。

## 验证入口

1. 启动服务（Mock 模式即可，无需 API Key）：

   ```powershell
   python -m uvicorn agent_system.app:app --host 127.0.0.1 --port 8000
   ```

2. 浏览器打开 `http://127.0.0.1:8000/`（或静态托管 `static/index.html` 的同源服务）。
3. 逐项执行 `docs/plan/phase4-step-4-notes.md` 第 7 节“手动验证清单”。

## 两条数据来源的切换

- 默认（流式）：`/` → `fetch("/chat/stream")`，route 来自 NDJSON `route` 事件；
- 非流式：`/?mode=chat` → `fetch("/chat")`，route 来自响应体 `route` 字段。

## 旧后端兼容验证

临时移除后端的 `route` 输出（或使用阶段四步骤 2 之前的镜像）后刷新页面：
assistant 消息旁应**没有任何标签**，顶部状态栏显示“未路由”，其余交互与改动前一致。

## 相关文档

- 设计与降级策略：`docs/plan/phase4-step-4-notes.md`
- 改造前基线：`docs/baseline/static-index-original.html`

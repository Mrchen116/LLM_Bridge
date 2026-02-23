# Session Inspector Frontend

`frontend/session-inspector` 是 Session Inspector 的前端工程（React + TypeScript + Vite）。

该前端构建后直接输出到后端静态目录 `src/inspector_ui`，由 FastAPI 在
`/ui/session-inspector/assets` 路径托管。

## 本地开发

```bash
cd frontend/session-inspector
npm install
npm run dev
```

默认 Vite 开发地址：`http://127.0.0.1:5173`（以实际启动输出为准）。

## 构建与测试

```bash
cd frontend/session-inspector
npm test
npm run build
```

说明：

- `npm test` 使用 `vitest run`
- `npm run build` 产物写入 `../../src/inspector_ui`

## 目录结构

- `src/components/session-list`：左侧 session 列表
- `src/components/timeline-lanes`：中间 lane 时间线
- `src/components/event-card`：时间线事件卡片
- `src/components/detail-panel`：右侧事件详情与日志文件弹窗
- `src/state`：`reducer + actions + types` 状态层
- `src/api`：后端 API 契约与客户端
- `src/lib`：纯函数解析/聚类模块（含单测）
- `src/business/use-session-inspector-controller.ts`：状态、请求、视图组装编排

## 设计约束（迁移友好）

- 状态变更统一走 reducer/action，不在 DOM 回调里直接改业务数据
- 视图层只消费 API 契约（`SessionSummary`、`TimelineResponse` 等）
- 解析/聚类逻辑保持纯函数，便于复用与测试
- 样式使用 token（CSS variables）统一管理
- 业务层与渲染层分离，便于后续继续演进

## 关键交互

- 时间线默认 dense 模式（无切换按钮）
- 点击事件只在右侧详情展示，不在时间线卡片内展开
- 日志文件支持“查看”弹窗：
  - `渲染换行`：把 `\n` 等按可读换行显示
  - `原始文本`：保留原始 JSON 字面量显示

## 后端联调

在仓库根目录启用 UI：

```bash
python start_proxy.py --ui
```

然后访问：

- 页面：`http://127.0.0.1:4000/ui/session-inspector`
- API：`/api/session-inspector/*`

# LLM_Bridge
<p align="center">
  <img src="logo.svg" alt="LLM_Bridge Logo" />
</p>

<p align="center">
  <a href="https://deepwiki.com/Mrchen116/LLM_Bridge">
    <img src="https://img.shields.io/badge/Ask-DeepWiki-blue?style=for-the-badge" alt="Ask DeepWiki" />
  </a>
</p>

一个基于 FastAPI 的多上游 LLM 代理服务，支持在同一入口下转发与桥接：

- Anthropic Messages：`/v1/messages`
- OpenAI Chat Completions：`/v1/chat/completions`
- OpenAI Responses：`/v1/responses`

支持按 `profile:model` 语法路由到不同上游，并提供请求/响应日志落盘、限流重试、Codex OAuth 适配，以及 Session Inspector（多 agent 时间线分析 UI）。

## 功能特性

- 统一入口代理 Anthropic / OpenAI 协议
- 按 profile 动态选择上游与默认模型
- 对 `406` / `429` 自动指数退避重试
- 支持流式（SSE）与非流式透传
- 可选屏蔽 Task 工具描述中的 `- Explore:` 行
- 支持会话日志与统计接口（session logs + token 聚合）
- 内置 Session Inspector：
  - 多 agent lane 时间线展示
  - 工具调用参数与工具定义（name/description/schema）查看
  - 非工具事件摘要查看
  - 日志文件原文弹窗查看（支持“渲染换行 / 原始文本”模式切换）

## 环境要求

- Python 3.10+

依赖已写入 `requirements.txt`。

## 快速开始

1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. 配置环境变量（示例）

```bash
export MOONSHOT_API_KEY="your_key"
export PROXY_HOST="127.0.0.1"
export PROXY_PORT="4000"
```

如果使用 `codex_oauth` profile，请先登录本地账号池（不再支持 `CODEX_ACCESS_TOKEN` / `CODEX_ACCOUNT_ID` 环境变量直传）：

```bash
python manage_codex_accounts.py add --label work --method browser
python manage_codex_accounts.py list
# 或直接进入交互式向导：
python manage_codex_accounts.py
```

3. 启动服务

```bash
python start_proxy.py
```

可选参数：

- `--ban_explore`：移除 Task 工具描述中的 `- Explore:` 行
- `--ban_stream`：禁用 `/v1/messages` 流式请求
- `--ui`：启用 Session Inspector UI 与 API
- `--open-ui`：启用 UI，并自动打开浏览器（`/ui/session-inspector`）

## Session Inspector（多 Agent 会话分析）

启用方式（二选一）：

- 环境变量：`export ENABLE_SESSION_INSPECTOR_UI=true`
- 启动参数：`python start_proxy.py --ui` 或 `python start_proxy.py --open-ui`

访问地址：

- 页面：`GET /ui/session-inspector`
- 静态资源：`/ui/session-inspector/assets/*`

### Agent Lane 判定规则（当前实现）

每个 turn 会先被转换到统一上下文（Responses 风格），然后按以下规则聚类 lane：

1. 先提取上下文键：`instructions`、`input`、`tools`、`tool_choice`、`reasoning`、`include`
2. 去掉当前 turn 最后一个 user suffix，只保留“当前请求之前的完整前缀”
3. `static_key` 只基于 `input` 之外的静态上下文 + `provider` + `model`
4. 在同 `static_key` 下，若“某已知 lane 的前缀”是“当前前缀”的前缀，则归入该 lane
5. 若没有命中，则创建新 lane

这保证了多 agent 并发场景下，能按历史前缀链和工具/系统上下文稳定区分 agent。

### 事件与详情

- 时间线事件类型：
  - `user_input`
  - `assistant_text`
  - `assistant_reasoning`
  - `tool_call`
  - `response_status`
- 事件详情可查看：
  - 事件摘要与完整内容
  - 工具参数
  - 工具定义（名称 / 描述 / 参数 schema）
  - 对应日志文件路径（request / response / non_stream / downstream）
- 日志文件弹窗支持：
  - `渲染换行`：将 `\n` 等转义序列按可读文本显示
  - `原始文本`：保留 JSON 原始字面量展示

## 上游配置

默认读取 `upstreams.json`，可用 `UPSTREAM_CONFIG_PATH` 覆盖路径。

关键字段：

- `defaultProfile`：默认上游 profile
- `profiles.<name>.provider`：`openai_compatible` / `anthropic` / `codex_oauth`
- `profiles.<name>.capabilities.ingress`：允许的入口协议
- `profiles.<name>.defaults.model`：默认模型

模型支持 `profile:model` 写法，例如：

- `moonshot:kimi-k2.5`
- `codexOAuth:gpt-5.2-codex`

## API 列表

- `GET /health`：健康检查
- `POST /v1/messages`：Anthropic Messages 入口（可桥接到 OpenAI 兼容上游）
- `POST /v1/messages/count_tokens`：token 估算
- `GET /session/{session_id}/stats`：会话统计
- `POST /v1/chat/completions`：OpenAI Chat Completions 入口
- `POST /v1/responses`：OpenAI Responses 入口

Session Inspector API（仅在 UI 启用时可用）：

- `GET /api/session-inspector/sessions`
- `GET /api/session-inspector/sessions/{session_id}/timeline`
- `GET /api/session-inspector/log-file?path=<logs/session 下的文件路径>`

`/api/session-inspector/log-file` 只允许读取 `logs/session` 目录内文件。

## 前端开发（Session Inspector）

前端位于 `frontend/session-inspector`（React + TypeScript + Vite）。

```bash
cd frontend/session-inspector
npm install
npm test
npm run build
```

构建产物会输出到后端静态目录 `src/inspector_ui/`，由 FastAPI 直接托管。

## 日志目录

运行后会在 `logs/` 下生成分类日志：

- `logs/anthropic/`
- `logs/openai/`
- `logs/session/`
- `logs/codeagent/`

## 测试

```bash
pytest -q
```

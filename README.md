# LLM_Bridge

[![Ask DeepWiki](https://deepwiki.com/Mrchen116/LLM_Bridge/badge.svg)](https://deepwiki.com/Mrchen116/LLM_Bridge)

一个基于 FastAPI 的多上游 LLM 代理服务，支持在同一入口下转发与桥接：

- Anthropic Messages：`/v1/messages`
- OpenAI Chat Completions：`/v1/chat/completions`
- OpenAI Responses：`/v1/responses`

支持按 `profile:model` 语法路由到不同上游，并提供请求/响应日志落盘、限流重试与 Codex OAuth 适配能力。

## 功能特性

- 统一入口代理 Anthropic / OpenAI 协议
- 按 profile 动态选择上游与默认模型
- 对 `406` / `429` 自动指数退避重试
- 支持流式（SSE）与非流式透传
- 可选屏蔽 Task 工具描述中的 `- Explore:` 行
- 支持会话日志与统计接口（session logs + token 聚合）

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

如果使用 `codex_oauth` profile，还可配置：

```bash
export CODEX_ACCESS_TOKEN="your_codex_access_token"
export CODEX_ACCOUNT_ID="your_account_id"
```

3. 启动服务

```bash
python start_proxy.py
```

可选参数：

- `--ban_explore`：移除 Task 工具描述中的 `- Explore:` 行
- `--ban_stream`：禁用 `/v1/messages` 流式请求

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

## 接口列表

- `GET /health`：健康检查
- `POST /v1/messages`：Anthropic Messages 入口（可桥接到 OpenAI 兼容上游）
- `POST /v1/messages/count_tokens`：token 估算
- `GET /session/{session_id}/stats`：会话统计
- `POST /v1/chat/completions`：OpenAI Chat Completions 入口
- `POST /v1/responses`：OpenAI Responses 入口

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


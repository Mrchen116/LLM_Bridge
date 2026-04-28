# 接入 Agent 客户端

本代理在同一地址上提供三种入口协议：

- **Anthropic Messages** — `POST /v1/messages`（常见于 Claude Code 等 Anthropic 兼容客户端）
- **OpenAI Chat Completions** — `POST /v1/chat/completions`
- **OpenAI Responses** — `POST /v1/responses`（Codex CLI 在 `wire_api = "responses"` 时走这条）

把 Agent 的 **API 地址** 指到运行 `python start_proxy.py` 的机器与端口（默认常是 `http://127.0.0.1:4000`，由 `PROXY_HOST` / `PROXY_PORT` 等决定）。具体走哪个上游由请求里的 **`profile:model`** 决定；`upstreams.json` 与 `@high` / `@low` 等后缀说明见主 README 的 **上游配置** 小节。

---

## Claude Code（Anthropic API 模式）

Claude Code 支持自定义 Anthropic 的 Base URL 与 Bearer Token，从而把请求发到本代理而不是 Anthropic 官方 API。

1. 先启动代理，并在 `upstreams.json` 里配好要用的 profile（例如 `codex_oauth` 账号池）。
2. 在启动 Claude Code 的终端中设置环境变量并运行：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="token"
export ANTHROPIC_DEFAULT_OPUS_MODEL="codexOAuth:gpt-5.4@high"
export ANTHROPIC_DEFAULT_SONNET_MODEL="codexOAuth:gpt-5.4"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="codexOAuth:gpt-5.4@low"
export ENABLE_TOOL_SEARCH=false
claude
```

**变量说明**

| 变量 | 作用 |
|------|------|
| `ANTHROPIC_BASE_URL` | Anthropic 客户端使用的 API 根地址。只写 **协议 + 主机 + 端口**，不要带 `/v1/messages`；客户端会自动拼 REST 路径。 |
| `ANTHROPIC_AUTH_TOKEN` | 会以 `Authorization: Bearer <值>` 发给**入口侧**。若本地代理未校验客户端密钥，常用固定占位（如 `token`）；若你在入口加了鉴权，需与代理配置一致。 |
| `ENABLE_TOOL_SEARCH` | 设为 `false` 可禁用 tool search，避免客户端发送上游提供方无法处理的请求（尤其代理到 OpenAI 兼容提供方时）。 |
| `ANTHROPIC_DEFAULT_*_MODEL` | Opus / Sonnet / Haiku 三档默认模型 ID。此处使用本网关识别的 **`profile:model`**（如 `codexOAuth:gpt-5.4`），并用 `@high` / `@low` 等后缀表达推理强度（与主 README 一致）。 |

**补充说明**

- 使用非官方 Anthropic 主机时，Claude Code 可能默认关闭部分能力（例如工具搜索）。是否需要 `ENABLE_TOOL_SEARCH=true` 等，以 [Claude Code 文档：LLM 网关 / 环境变量](https://docs.anthropic.com/en/docs/claude-code/llm-gateway) 为准。
- 可把上述 `export` 写进 shell 配置文件，或在 Claude Code 的 `settings.json` 里用 `env` 持久化。
- **Billing header 与 KV cache 影响。** Claude Code 每次请求都会在 system prompt 里附带 `x-anthropic-billing-header`，其中 `cch`（conversation context hash）值每轮都会变化。如果你代理到依赖前缀匹配来复用 KV cache 的推理后端（例如 vLLM 等带 prompt cache 的引擎），这个不断变化的 hash 会导致 system prompt 前缀无法命中缓存，使多轮对话的 KV cache 失效。Session inspector 在分析日志时会对该字段做归一化，但向上游转发的原始请求中仍包含变化值。
- **Tool search 兼容性。** 如果上游提供方不支持 Claude Code 的 tool search 能力（例如代理到 OpenAI 兼容提供方时），建议设置 `ENABLE_TOOL_SEARCH=false`，避免客户端发送提供方无法处理的 tool search 请求。

---

## OpenAI Codex CLI（自定义 provider + profile）

把本代理当作 **自定义** OpenAI 兼容提供方：`base_url` 指向代理的 **`/v1` 前缀**，`wire_api = "responses"` 与网关的 `POST /v1/responses` 对齐。

在 Codex 的配置文件（一般为 `~/.codex/config.toml`，以本机安装为准）中增加 provider 与 profile。

**提供方**

```toml
[model_providers.llm_proxy]
name = "LLM_PROXY"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
requires_openai_auth = false
```

**使用该提供方的 profile**

```toml
[profiles.proxy_gpt54]
model_provider = "llm_proxy"
model = "codexOAuth:gpt-5.4"
```

启动时指定 profile：

```bash
codex --profile proxy_gpt54
```

**注意**

- `model_provider` 必须与 `[model_providers.xxx]` 里的 `xxx` 一致（示例中为 `llm_proxy`）。
- `model` 须与 `upstreams.json` 中的 profile 名、模型名及可选的 `@effort` 后缀一致。
- 若入口或中间层需要 OpenAI 式 API Key，将 `requires_openai_auth` 设为 `true` 并在 Codex 侧配置密钥。

更多 Codex 配置项见官方文档，例如 [配置参考](https://developers.openai.com/codex/config-reference)、[高级配置](https://developers.openai.com/codex/config-advanced)。

---

## Session ID 识别方式

Session Inspector 按 session 聚合 turn。代理通过一套小型插件解析 session ID：每个插件实现 `extract(headers, body) -> Optional[str]`，按 `priority` 升序依次尝试，第一个返回非空字符串的结果即为最终 session ID。

内置插件（代码在 `src/session_id_plugins/`）：

| 文件 | 名称 | 优先级 | 读取位置 |
|------|------|--------|----------|
| `plugin_headers.py` | `builtin_headers` | 100 | HTTP 头 `X-Session-Id`、`x-session-id`、`session_id` |
| `plugin_metadata.py` | `builtin_metadata` | 200 | `body.metadata.user_id` — JSON `{"session_id":"..."}` 或旧格式 `session_<id>` |

### 哪些 Agent 原生携带 Session ID

| Agent | 注入方式 | 说明 |
|-------|----------|------|
| Claude Code | `x-session-id` header | 自动注入，无需额外配置 |
| Codex CLI | `session_id` header | 自动注入，无需额外配置 |
| opencode | — | 需要安装客户端插件（见下） |
| OpenClaw | — | 需要安装客户端插件（见下） |

### Agent 客户端插件（`agent-client-plugins/`）

对于不会主动注入 session ID 的 Agent，本仓库在 `agent-client-plugins/` 目录下提供了对应的客户端插件。每个子目录是一个独立的插件包，安装到对应的 Agent 后，会在每次 LLM 请求中自动注入 `X-Session-Id`。

子 Agent 的 session 归并同样已处理：当 Agent 通过 task 机制派生子 Agent 时（子 Agent 会获得一个新的 session ID），插件会沿 `parentID` 链向上追溯到根 session，并以根 session ID 作为 `X-Session-Id`，使整棵 Agent 树在 Inspector 中归入同一会话。

| 目录 | 目标 Agent | 安装方式 |
|------|-----------|---------|
| `agent-client-plugins/opencode/` | [opencode](https://opencode.ai) | 在 `opencode.json` 的 `plugin` 字段中添加该目录路径 |
| `agent-client-plugins/openclaw/` | OpenClaw | `openclaw plugins install /path/to/agent-client-plugins/openclaw` |

具体安装步骤见各目录下的 `GUIDE.md`。

### 为自定义 Agent 添加插件

1. 新建 `src/session_id_plugins/plugin_my_agent.py`：

```python
from src.session_id_plugins import register

class MyAgentPlugin:
    name = "my_agent"
    priority = 50  # 小于 100 时先于内置插件执行

    def extract(self, headers, body):
        return body.get("my_agent_session_id") or None

register(MyAgentPlugin())
```

2. 在 `src/session_id_plugins/__init__.py` 末尾增加一行导入以激活插件：

```python
from . import plugin_my_agent
```

无需再改其他文件。

---

## 其他 Agent

只要能直接调用上述三种 URL 之一，即可接入同一网关：

- **Anthropic 形态** → `POST /v1/messages`
- **OpenAI Chat 形态** → `POST /v1/chat/completions`
- **OpenAI Responses 形态** → `POST /v1/responses`

模型 ID 仍使用 `profile:model`（及可选 `@high` / `@low`），与主 README 保持一致即可。

---

## English version

English: [agent-integration.md](agent-integration.md).

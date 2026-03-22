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
claude
```

**变量说明**

| 变量 | 作用 |
|------|------|
| `ANTHROPIC_BASE_URL` | Anthropic 客户端使用的 API 根地址。只写 **协议 + 主机 + 端口**，不要带 `/v1/messages`；客户端会自动拼 REST 路径。 |
| `ANTHROPIC_AUTH_TOKEN` | 会以 `Authorization: Bearer <值>` 发给**入口侧**。若本地代理未校验客户端密钥，常用固定占位（如 `token`）；若你在入口加了鉴权，需与代理配置一致。 |
| `ANTHROPIC_DEFAULT_*_MODEL` | Opus / Sonnet / Haiku 三档默认模型 ID。此处使用本网关识别的 **`profile:model`**（如 `codexOAuth:gpt-5.4`），并用 `@high` / `@low` 等后缀表达推理强度（与主 README 一致）。 |

**补充说明**

- 使用非官方 Anthropic 主机时，Claude Code 可能默认关闭部分能力（例如工具搜索）。是否需要 `ENABLE_TOOL_SEARCH=true` 等，以 [Claude Code 文档：LLM 网关 / 环境变量](https://docs.anthropic.com/en/docs/claude-code/llm-gateway) 为准。
- 可把上述 `export` 写进 shell 配置文件，或在 Claude Code 的 `settings.json` 里用 `env` 持久化。

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

## 其他 Agent

只要能直接调用上述三种 URL 之一，即可接入同一网关：

- **Anthropic 形态** → `POST /v1/messages`
- **OpenAI Chat 形态** → `POST /v1/chat/completions`
- **OpenAI Responses 形态** → `POST /v1/responses`

模型 ID 仍使用 `profile:model`（及可选 `@high` / `@low`），与主 README 保持一致即可。

---

## English version

English: [agent-integration.md](agent-integration.md).

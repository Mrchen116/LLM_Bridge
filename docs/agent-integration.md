# Agent client integration

This gateway speaks three ingress protocols on one host:

- **Anthropic Messages** — `POST /v1/messages` (typical for Claude Code and other Anthropic-compatible agents)
- **OpenAI Chat Completions** — `POST /v1/chat/completions`
- **OpenAI Responses** — `POST /v1/responses` (typical for Codex CLI when `wire_api = "responses"`)

Point your agent at the **same host and port** where you run `python start_proxy.py` (defaults: `PROXY_HOST` / `PROXY_PORT`, often `http://127.0.0.1:4000`). Upstream routing uses the `profile:model` string from the client; see the main README section **Upstream Configuration** for `upstreams.json` and suffixes like `@high` / `@low`.

---

## Claude Code (Anthropic API mode)

Claude Code can use a custom Anthropic base URL and bearer token so traffic goes to this proxy instead of Anthropic’s cloud API.

1. Start the proxy and ensure the profile you want (for example a `codex_oauth` pool) is configured in `upstreams.json`.
2. In the shell where you launch Claude Code, set:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:4000"
export ANTHROPIC_AUTH_TOKEN="token"
export ANTHROPIC_DEFAULT_OPUS_MODEL="codexOAuth:gpt-5.4@high"
export ANTHROPIC_DEFAULT_SONNET_MODEL="codexOAuth:gpt-5.4"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="codexOAuth:gpt-5.4@low"
claude
```

**What each variable does**

| Variable | Role |
|----------|------|
| `ANTHROPIC_BASE_URL` | API root for the Anthropic client. Use **scheme + host + port** only (no `/v1/messages`); the client appends the REST paths. |
| `ANTHROPIC_AUTH_TOKEN` | Sent as `Authorization: Bearer <value>`. Use whatever your deployment expects at the **ingress** (many local setups use a fixed placeholder such as `token` if the proxy does not enforce client authentication). |
| `ANTHROPIC_DEFAULT_*_MODEL` | Default model IDs for Opus / Sonnet / Haiku tiers. Here they are **`profile:model`** strings resolved by this gateway (for example `codexOAuth:gpt-5.4` and reasoning effort via `@high` / `@low`). |

**Notes**

- If you rely on a non-default Anthropic API host, Claude Code may disable some features (for example tool search) unless you opt in per [Claude Code environment variables](https://docs.anthropic.com/en/docs/claude-code/llm-gateway) (e.g. `ENABLE_TOOL_SEARCH=true` when your proxy forwards the relevant payloads).
- You can persist the same variables in your shell profile or in Claude Code’s `settings.json` under `env` so every session picks them up.

---

## OpenAI Codex CLI (custom provider + profile)

Codex can target this gateway as a **custom** OpenAI-compatible provider: set `base_url` to the proxy’s **`/v1` prefix** and use `wire_api = "responses"` so the CLI uses the Responses-shaped wire that matches `POST /v1/responses`.

Add a provider block and a profile in `~/.codex/config.toml` (path may vary by install; it is the Codex CLI config file).

**Provider**

```toml
[model_providers.llm_proxy]
name = "LLM_PROXY"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"
requires_openai_auth = false
```

**Profile that uses that provider**

```toml
[profiles.proxy_gpt54]
model_provider = "llm_proxy"
model = "codexOAuth:gpt-5.4"
```

Run Codex with that profile:

```bash
codex --profile proxy_gpt54
```

**Notes**

- `model_provider` must match the TOML table name (`llm_proxy` ↔ `[model_providers.llm_proxy]`).
- `model` should be a gateway model string (`profile:model`, optional `@effort` suffix) consistent with your `upstreams.json`.
- Set `requires_openai_auth = true` and configure the CLI’s API key only if your proxy or an intermediate layer requires an OpenAI-style key on requests.

For more Codex configuration options, see the official references such as [Configuration reference](https://developers.openai.com/codex/config-reference) and [Advanced configuration](https://developers.openai.com/codex/config-advanced).

---

## Other agents

Any client that can call one of the three ingress URLs above can use the same gateway. Use:

- **Anthropic-shaped** clients → `POST /v1/messages`
- **OpenAI Chat** clients → `POST /v1/chat/completions`
- **OpenAI Responses** clients → `POST /v1/responses`

Keep model IDs aligned with `profile:model` (and optional `@high` / `@low`) as documented in the main README.

---

## Chinese version

Simplified Chinese: [agent-integration-zh.md](agent-integration-zh.md).

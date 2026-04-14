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

## Session ID identification

The Session Inspector groups turns by session. The proxy resolves a session ID via a small plugin system: each plugin implements `extract(headers, body) -> Optional[str]`, plugins are tried in ascending `priority`, and the first non-empty result wins.

Built-in plugins (under `src/session_id_plugins/`):

| File | Name | Priority | Reads |
|------|------|----------|--------|
| `plugin_headers.py` | `builtin_headers` | 100 | Headers `X-Session-Id`, `x-session-id`, `session_id` |
| `plugin_metadata.py` | `builtin_metadata` | 200 | `body.metadata.user_id` — JSON `{"session_id":"..."}` or legacy `session_<id>` |

### Which agents send session IDs natively

| Agent | Method | Notes |
|-------|--------|-------|
| Claude Code | `x-session-id` header | Injected automatically; no extra setup |
| Codex CLI | `session_id` header | Injected automatically; no extra setup |
| opencode | — | Needs a client-side plugin (see below) |
| OpenClaw | — | Needs a client-side plugin (see below) |

### Agent client plugins (`agent-client-plugins/`)

For agents that do not inject a session ID on their own, this repo ships small client-side plugins under `agent-client-plugins/`. Each sub-directory is a self-contained package; install it into the corresponding agent once and it will inject `X-Session-Id` on every LLM request.

Sub-agent session grouping is also handled: when an agent spawns a sub-agent (a new session with a different ID), the plugin traces the `parentID` chain back to the root session and uses that root ID as `X-Session-Id`, so the entire agent tree is grouped under one session in the Inspector.

| Directory | Target agent | Install method |
|-----------|-------------|----------------|
| `agent-client-plugins/opencode/` | [opencode](https://opencode.ai) | Add the directory path to `plugin` in `opencode.json` |
| `agent-client-plugins/openclaw/` | OpenClaw | `openclaw plugins install /path/to/agent-client-plugins/openclaw` |

See the `GUIDE.md` inside each directory for full installation steps.

### Adding a plugin for your own agent

1. Create `src/session_id_plugins/plugin_my_agent.py`:

```python
from src.session_id_plugins import register

class MyAgentPlugin:
    name = "my_agent"
    priority = 50  # lower than 100 = tried before built-in plugins

    def extract(self, headers, body):
        return body.get("my_agent_session_id") or None

register(MyAgentPlugin())
```

2. Import it at the bottom of `src/session_id_plugins/__init__.py`:

```python
from . import plugin_my_agent
```

No other files need to change.

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

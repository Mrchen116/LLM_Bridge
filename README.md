# LLM_Bridge: One Gateway for Multi-Upstream LLMs, Protocol Translation, and Agent Observability

<p align="center">
  <img src="logo.svg" alt="LLM_Bridge Logo" />
</p>

<p align="center">
  <a href="README-zh.md">
    <img src="https://img.shields.io/badge/Docs-%E4%B8%AD%E6%96%87-red?style=for-the-badge" alt="Chinese README" />
  </a>
  <a href="https://deepwiki.com/Mrchen116/LLM_Bridge">
    <img src="https://img.shields.io/badge/Ask-DeepWiki-blue?style=for-the-badge" alt="Ask DeepWiki" />
  </a>
</p>

![Session Inspector Main UI](docs/images/session-inspector-main-ui.png)
![Session Inspector Stats UI](docs/images/session-inspector-stats-ui.png)

Multi-upstream LLM proxy layer for Agent and Coding Assistant workloads. It consolidates multiple model providers, multiple Codex OAuth subscriptions, three common protocol entrypoints, and session observability into a single FastAPI service.

Supported ingress endpoints:

- Anthropic Messages: `/v1/messages`
- OpenAI Chat Completions: `/v1/chat/completions`
- OpenAI Responses: `/v1/responses`

Think of it as an LLM gateway built specifically for agent systems:

- Expose one stable endpoint externally while routing internally by `profile:model`
- Pool multiple Codex subscriptions and fail over between accounts when needed
- Bridge between `Anthropic Messages`, `OpenAI Chat Completions`, and `OpenAI Responses`
- Preserve full multi-agent traces and inspect them visually
- Automatically retry around `406` and `429` responses to reduce agent interruption

## What Problems It Solves

- You want one place to manage multiple upstream LLM APIs instead of hardcoding keys, models, and protocols across several agent tools
- You have multiple Codex subscriptions and want to turn them into a resource pool with automatic switching on rate limits, expiry, or account instability
- You want clients that speak one protocol to use models or internal services that expose another
- You need to inspect multi-agent runs, including tool calls, context drift, failure points, and token usage
- You want to collect black-box agent trajectories and store requests, responses, tool calls, and session structure for SFT or RL datasets

## Why Use It

- Unified upstream management: one gateway for commercial models, internal deployments, and Codex OAuth account pools
- Protocol interoperability: bridge `Anthropic Messages`, `OpenAI Chat Completions`, and `OpenAI Responses`
- Agent compatibility: use a Codex subscription behind Claude Code style requests, or expose a single-protocol internal deployment to multiple agent clients
- Visual trace inspection: built-in Session Inspector for lanes, tool arguments, tool definitions, summary events, and raw logs
- Black-box trajectory capture: session logs are persisted for replay, auditing, incident analysis, and dataset preparation
- Rate-limit resilience: exponential backoff for `406` and `429`, plus Codex OAuth account failover
- Practical integration: supports both SSE streaming and non-stream passthrough so it can sit in front of existing agent stacks

## Features

- Route dynamically by `profile:model`
- Manage local Codex OAuth account pools with per-request failover
- Support both SSE streaming and non-stream passthrough
- Optionally strip `- Explore:` lines from Task tool descriptions
- Expose session logs and stats APIs, including token aggregation
- Built-in Session Inspector:
  - Multi-agent lane timeline view
  - Tool arguments and tool definition inspection (`name` / `description` / schema)
  - Summary views for non-tool events
  - Raw log modal with rendered newline / raw text modes

## Requirements

- Python 3.10+

Dependencies are listed in `requirements.txt`.

## Quick Start

1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure environment variables

```bash
export MOONSHOT_API_KEY="your_key"
export PROXY_HOST="127.0.0.1"
export PROXY_PORT="4000"
```

If you use a `codex_oauth` profile, log into the local account pool first. Direct `CODEX_ACCESS_TOKEN` / `CODEX_ACCOUNT_ID` environment-variable injection is no longer supported:

```bash
python manage_codex_accounts.py add --label work --method browser
python manage_codex_accounts.py list
# Or enter the interactive wizard:
python manage_codex_accounts.py
```

3. Start the service

```bash
python start_proxy.py
```

Optional flags:

- `--ban_explore`: remove `- Explore:` lines from Task tool descriptions
- `--ban_stream`: disable streaming for `/v1/messages`
- `--ui`: enable the Session Inspector UI and API
- `--open-ui`: enable the UI and open the browser automatically at `/ui/session-inspector`

## Session Inspector

Enable it in one of two ways:

- Environment variable: `export ENABLE_SESSION_INSPECTOR_UI=true`
- Startup flag: `python start_proxy.py --ui` or `python start_proxy.py --open-ui`

Endpoints:

- Page: `GET /ui/session-inspector`
- Static assets: `/ui/session-inspector/assets/*`

### Agent Lane Grouping Rules

Each turn is first normalized into a unified context (Responses-style), then grouped into lanes with the following rules:

1. Extract context keys: `instructions`, `input`, `tools`, `tool_choice`, `reasoning`, `include`
2. Remove the current turn's final user suffix and keep only the full prefix before the current request
3. Build `static_key` from static context outside `input`, plus `provider` and `model`
4. Under the same `static_key`, assign the turn to a known lane if that lane's prefix is a prefix of the current prefix
5. If nothing matches, create a new lane

This keeps agent lanes stable in concurrent multi-agent runs by using prefix history plus tool/system context.

### Events and Details

- Timeline event types:
  - `user_input`
  - `assistant_text`
  - `assistant_reasoning`
  - `tool_call`
  - `response_status`
- Detail views include:
  - Event summary and full content
  - Tool arguments
  - Tool definitions (name / description / parameter schema)
  - Associated log file paths (`request` / `response` / `non_stream` / `downstream`)
- Log modal modes:
  - `Rendered newlines`: displays `\n` and other escaped sequences as readable text
  - `Raw text`: shows the original JSON literal content

## Upstream Configuration

By default, configuration is loaded from `upstreams.json`. You can override the path with `UPSTREAM_CONFIG_PATH`.

Key fields:

- `defaultProfile`: default upstream profile
- `profiles.<name>.provider`: `openai_compatible` / `anthropic` / `codex_oauth`
- `profiles.<name>.capabilities.ingress`: allowed ingress protocols
- `profiles.<name>.defaults.model`: default model

Models support the `profile:model` syntax, for example:

- `moonshot:kimi-k2.5`
- `codexOAuth:gpt-5.2-codex`

For `codex_oauth` profiles, you can optionally append a reasoning effort suffix with `@`:

- `codexOAuth:gpt-5.4@high` — 指定推理强度为 high
- `codexOAuth:byenv@high` — 使用 profile 默认模型，推理强度为 high
- `byenv@high` — 使用 defaultProfile 的默认模型，推理强度为 high

When the downstream request does not provide `reasoning_effort` / `reasoning.effort`, the model suffix is used. Explicit parameters take precedence over the suffix.

## API Endpoints

- `GET /health`: health check
- `POST /v1/messages`: Anthropic Messages ingress, can bridge to OpenAI-compatible upstreams
- `POST /v1/messages/count_tokens`: token estimation
- `GET /session/{session_id}/stats`: session statistics
- `POST /v1/chat/completions`: OpenAI Chat Completions ingress
- `POST /v1/responses`: OpenAI Responses ingress

Session Inspector APIs are available only when the UI is enabled:

- `GET /api/session-inspector/sessions`
- `GET /api/session-inspector/sessions/{session_id}/timeline`
- `GET /api/session-inspector/log-file?path=<path under logs/session>`

`/api/session-inspector/log-file` only allows reads inside `logs/session`.

## Frontend Development

The frontend lives in `frontend/session-inspector` and is built with React, TypeScript, and Vite.

```bash
cd frontend/session-inspector
npm install
npm test
npm run build
```

Build output is emitted to the backend static directory `src/inspector_ui/`, which is served directly by FastAPI.

## Log Directories

Running the service creates logs under:

- `logs/session/`: per-session turn logs used by Session Inspector
- `logs/raw/`: raw request/response/header captures, grouped by bucket such as `anthropic`, `openai_chat`, and `openai_codex`

## Tests

```bash
pytest -q
```

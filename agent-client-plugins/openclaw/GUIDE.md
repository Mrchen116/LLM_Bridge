# OpenClaw Session Header Plugin

为 LLM_Bridge 的 provider 的每次 LLM 请求注入 `X-Session-Id` HTTP header，供 LLM_Bridge 按会话聚合日志。

## 背景

OpenClaw 内部用 UUID 标识每个对话 session（同一上下文的连续多轮对话共享同一个 sessionId）。但当 OpenClaw 通过 OpenAI-compatible 接口调用 LLM_Bridge 时，这个 sessionId 默认不会出现在 HTTP 请求里，导致 LLM_Bridge 无法区分哪些请求属于同一对话。

## 原理

```
OpenClaw                         LLM_Bridge
────────────────────────         ─────────────────────────
对话 session                     请求日志
  sessionId: "abc-123"           请求 1: X-Session-Id: abc-123  ─┐
    turn 1 ──→ HTTP 请求  ──→                                     ├─ 同一会话
    turn 2 ──→ HTTP 请求  ──→    请求 2: X-Session-Id: abc-123  ─┘
    turn 3 ──→ HTTP 请求  ──→    请求 3: X-Session-Id: abc-123  ─┘

对话 session
  sessionId: "xyz-456"
    turn 1 ──→ HTTP 请求  ──→    请求 4: X-Session-Id: xyz-456  ─ 另一个会话
```

### 调用链（已通过源码验证）

1. OpenClaw 为每个对话创建一个 UUID `sessionId`，存在 agent 的 `AgentLoopConfig` 里
2. `agent-loop.ts` 调用 `streamFn(model, context, { ...config })` 时，`sessionId` 随 config 展开进入 `options`
3. OpenClaw 的 `extra-params.ts` 在调用 stream 前执行 `wrapProviderStreamFn`，触发本插件的 `wrapStreamFn` hook
4. 本插件从 `options.sessionId` 读取值，写入 `options.headers["X-Session-Id"]`
5. `openai-completions.ts` 将 `options.headers` 传给 OpenAI HTTP client（`createClient` 第四个参数）
6. HTTP 请求发出，LLM_Bridge 收到带有 `X-Session-Id` header 的请求
7. LLM_Bridge 的 `HeadersPlugin`（内置，优先级 100）提取该 header 作为 session-id

## 文件结构

```
openclaw/
├── package.json          # 插件包定义
├── openclaw.plugin.json  # OpenClaw 插件清单
├── index.ts              # 插件逻辑（wrapStreamFn hook）
└── GUIDE.md              # 本文件
```

## 安装

将插件目录克隆或下载到本地后，在插件目录内执行：

### 方式一：直接安装（推荐）

```bash
openclaw plugins install /path/to/agent-client-plugins/openclaw
```

### 方式二：开发模式（link，修改后无需重装）

```bash
openclaw plugins install -l /path/to/agent-client-plugins/openclaw
```

### 安装后重启 gateway

```bash
openclaw gateway restart
```

### 验证安装

```bash
openclaw plugins list
# 应看到 session-header 在列表中
```

## Provider ID 配置

插件默认对 provider id 为 `llm-bridge` 的请求生效。如果你在 OpenClaw 中给 LLM_Bridge 配置了不同的 provider id，需同步修改 `index.ts` 和 `openclaw.plugin.json` 中的 `id` 字段，然后重新安装插件。

## LLM_Bridge 侧

LLM_Bridge 内置的 `HeadersPlugin`（`src/session_id_plugins/plugin_headers.py`）已支持提取 `X-Session-Id`，**无需额外修改**。

## 卸载

```bash
openclaw plugins uninstall session-header
openclaw gateway restart
```

## 注意事项

- 本插件只对配置的 provider id 生效；其他 provider 不受影响
- `sessionId` 为空时（极少发生）插件直接透传请求，不注入 header

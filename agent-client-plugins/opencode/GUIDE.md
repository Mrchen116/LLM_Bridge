# opencode LLM Bridge Session Plugin

为 opencode 的每次 LLM 请求注入 `X-Session-Id` HTTP header，供 LLM_Bridge 按会话聚合日志。

## 背景

opencode 内部为每个对话维护一个 SessionID。当主 agent 通过 `task` 工具派生子 agent 时，子 agent 会获得一个新的 SessionID，并通过 `parentID` 字段指向父 session。

opencode 向第三方 LLM Provider（如 LLM_Bridge 的 OpenAI-compatible 接口）发起请求时，不会携带任何 session 信息。本插件通过 `chat.headers` hook 注入 `X-Session-Id` header，使 LLM_Bridge 能够识别并聚合同一会话下所有请求的日志。

## 主/子 Agent 的 Session 归并

主 agent 和其派生的子 agent（`task` 工具）拥有不同的 SessionID，但通过 `parentID` 链接到同一棵会话树。本插件会向上遍历 `parentID` 链，找到根 session，并统一使用根 SessionID 作为 `X-Session-Id`，确保整棵会话树的所有 LLM 请求都归入同一个会话日志。

```
opencode                              LLM_Bridge
─────────────────────────────         ────────────────────────
主 agent (session: root-111)
  turn 1 ──→ HTTP 请求  ──────→       X-Session-Id: root-111 ─┐
  turn 2 ──→ HTTP 请求  ──────→       X-Session-Id: root-111  │
  [派生子 agent]                                               │ 同一会话
    子 agent (session: child-222,      X-Session-Id: root-111  │ (root-111)
              parentID: root-111)                              │
      turn 1 ──→ HTTP 请求  ──→       X-Session-Id: root-111 ─┘
```

## 实现原理

1. `chat.headers` hook 在每次 LLM 调用前触发，携带当前 `sessionID`
2. 插件通过 opencode 内置 SDK client 调用 session API，获取 `parentID`
3. 递归向上查找，直到根 session（无 `parentID`）
4. 将根 SessionID 写入 `X-Session-Id` header
5. 查找结果会被缓存，避免同一 session 重复查询

## 文件结构

```
opencode/
├── package.json   # 插件包定义
├── index.ts       # 插件逻辑
└── GUIDE.md       # 本文件
```

## 安装

在 opencode 的配置文件（`~/.config/opencode/opencode.json` 或项目级 `opencode.json`）中添加插件路径：

```json
{
  "plugin": [
    "file:///path/to/agent-client-plugins/opencode"
  ]
}
```

Windows 示例：

```json
{
  "plugin": [
    "file:///C:/Users/xxx/path/to/agent-client-plugins/opencode"
  ]
}
```

重启 opencode 后生效。

## 模型配置

本插件的作用是在请求到达 LLM_Bridge 时注入 session header。**前提是所有 agent 的 LLM 请求都必须经过 LLM_Bridge**。

### 基本配置

opencode 支持两种方式将请求转发给 LLM_Bridge，取决于你的 LLM_Bridge 对外暴露的 API 格式。

#### 方式一：OpenAI-compatible 格式

LLM_Bridge 对外提供 OpenAI 兼容接口（`/v1/chat/completions`）时，使用自定义 provider：

```json
{
  "provider": {
    "my_bridge": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LLM Bridge",
      "options": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "opencode"
      },
      "models": {
        "my-model": { "name": "My Model" }
      }
    }
  },
  "model": "my_bridge/my-model"
}
```

oh-my-openagent 中对应写法：`"model": "my_bridge/my-model"`

#### 方式二：Anthropic 格式

LLM_Bridge 对外提供 Anthropic 兼容接口（`/v1/messages`）时，覆盖内置 `anthropic` provider 的 `baseURL`：

```json
{
  "provider": {
    "anthropic": {
      "options": {
        "baseURL": "http://127.0.0.1:4000/v1",
        "apiKey": "opencode"
      },
      "models": {
        "my-model": { "name": "My Model" }
      }
    }
  },
  "model": "anthropic/my-model"
}
```

oh-my-openagent 中对应写法：`"model": "anthropic/my-model"`

> **注意**：两种方式的 `providerID` 不同——前者是你自定义的名称（如 `my_bridge`），后者固定为 `anthropic`。oh-my-openagent 的 model 字段必须与此保持一致，否则子 agent 会回退到内置 provider 绕过 LLM_Bridge。

### 使用 oh-my-opencode / oh-my-openagent

oh-my-opencode（oh-my-openagent）为每个 agent 单独指定模型。若配置不当，子 agent 会使用内置的 `opencode` provider（如 `opencode/gpt-5-nano`），**完全绕过 LLM_Bridge**，导致子 agent 的请求没有日志。

#### 正确配置方式

在 `oh-my-opencode.json` / `oh-my-openagent.json` 中，agent 的 `model` 字段必须使用 `providerID/modelID` 的完整格式，其中 `providerID` 是你在 opencode 里指向 LLM_Bridge 的 provider 名称：

```json
{
  "agents": {
    "sisyphus": { "model": "my_bridge/my-model" },
    "explore":  { "model": "my_bridge/my-model" },
    "oracle":   { "model": "my_bridge/my-model" }
  },
  "categories": {
    "quick":    { "model": "my_bridge/my-model" },
    "deep":     { "model": "my_bridge/my-model" }
  }
}
```

#### 常见错误

| 写法 | 结果 |
|------|------|
| `"opencode/gpt-5-nano"` | 走内置 provider，**绕过 LLM_Bridge**，无日志 |
| `"my-model"` | 格式缺少 provider 前缀，解析失败后回退到内置 provider |
| `"my_bridge/my-model"` | 正确，走 LLM_Bridge ✓ |

#### 验证方式

重启 opencode 并触发一次带子 agent 的对话，检查 LLM_Bridge 日志目录：

- 若子 agent 的请求出现在**主 session 的同一文件夹**下 → 配置正确
- 若子 agent 的请求**不存在任何日志** → agent 使用了绕过 LLM_Bridge 的 provider

## LLM_Bridge 侧

LLM_Bridge 内置的 `HeadersPlugin`（`src/session_id_plugins/plugin_headers.py`）已支持提取 `X-Session-Id`，**无需额外修改**。

## 注意事项

- 若 opencode 的 session API 不可达（极少发生），插件会降级为使用当前 `sessionID`，不影响正常功能
- 插件对所有 provider 均生效，不限于 LLM_Bridge
- **子 agent 的请求必须走同一 LLM_Bridge provider**，否则 session 归并无意义；请确保所有 oh-my-opencode agent 的 model 均使用 `providerID/modelID` 完整格式

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

## LLM_Bridge 侧

LLM_Bridge 内置的 `HeadersPlugin`（`src/session_id_plugins/plugin_headers.py`）已支持提取 `X-Session-Id`，**无需额外修改**。

## 注意事项

- 若 opencode 的 session API 不可达（极少发生），插件会降级为使用当前 `sessionID`，不影响正常功能
- 插件对所有 provider 均生效，不限于 LLM_Bridge

# macOS 登录自启动：launchd + tmux

这套配置会在 macOS 用户登录后启动代理：创建名为 `LLM` 的 detached tmux 会话，并在其中执行：

```bash
.venv/bin/python -u start_proxy.py --ui
```

仓库内的启动脚本是 `scripts/start_proxy_tmux.sh`，它会：

- 根据脚本自身位置确定仓库目录，确保从正确位置读取 `.env`、`upstreams.json` 和 `.codex_oauth.json`；
- 使用仓库自己的 `.venv`，而不是 macOS 系统 Python；
- 若名为 `LLM` 的 tmux 会话已经存在，则直接成功退出，避免重复启动；
- 同时兼容 Apple Silicon 和 Intel Mac 上 Homebrew 安装的 tmux；
- 在 tmux 中显示控制台输出，同时追加写入 `logs/proxy-console.log`。

启动时若 Codex access token 需要 refresh，而登录瞬间网络/代理尚未就绪，`start_proxy.py` 会在默认 **180 秒**窗口内按 **15 秒**间隔自动重试（日志前缀 `[codex-auth]`）。可用环境变量覆盖：

- `CODEX_STARTUP_RETRY_SECONDS`（默认 `180`；`0` = 只试一次）
- `CODEX_STARTUP_RETRY_INTERVAL_SECONDS`（默认 `15`）

未配置启用账号、或账号缺少 `refresh_token` 等本地配置问题会立即失败，不会空等。

这里使用的是“用户登录后启动”的 LaunchAgent，不是“登录前启动”的系统 LaunchDaemon。tmux、仓库环境变量和 Codex OAuth 状态都属于当前用户，因此用户级 LaunchAgent 更合适。

## 前置条件

在仓库根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage_codex_accounts.py list
```

如果尚未安装 tmux：

```bash
brew install tmux
```

## 安装 LaunchAgent

创建 `~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist`。把下面两处 `/absolute/path/to/LLM_PROXY` 替换为当前仓库的绝对路径；launchd 不会展开 `~`。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.github.mrchen116.llm-bridge</string>

  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/to/LLM_PROXY/scripts/start_proxy_tmux.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/absolute/path/to/LLM_PROXY</string>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/your-name/Library/Logs/llm-bridge-launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/your-name/Library/Logs/llm-bridge-launchd.err.log</string>
</dict>
</plist>
```

校验并加载：

```bash
chmod +x scripts/start_proxy_tmux.sh
plutil -lint ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl kickstart -k "gui/$(id -u)/io.github.mrchen116.llm-bridge"
```

如果服务已经加载，且刚修改过 plist，需要先卸载旧配置再重新加载：

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
```

## 验证与日常操作

```bash
tmux list-sessions
tmux attach -t LLM
curl -fsS http://127.0.0.1:4000/health
```

在 tmux 中按 `Ctrl-b d` 可以脱离会话；在 pane 内执行 `exit` 会停止代理并关闭会话。

修改 `.env`、`upstreams.json` 或 OAuth 账号配置后，需要重启 tmux 会话，代理才会重新读取：

```bash
tmux kill-session -t LLM
launchctl kickstart -k "gui/$(id -u)/io.github.mrchen116.llm-bridge"
```

不进入 tmux 也可以查看最近输出：

```bash
tmux capture-pane -p -t LLM:0.0 -S -100
tail -f logs/proxy-console.log
```

## 停用

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
tmux kill-session -t LLM
```

不要给这套 LaunchAgent 增加 `KeepAlive=true`。detached tmux 客户端创建会话后会立即退出，launchd 会因此反复调用启动脚本。如果需要进程崩溃后自动拉起，应改成由 launchd 直接以前台方式托管 Python 并设置 `KeepAlive`，而不是把 `KeepAlive` 和 detached tmux 组合使用。

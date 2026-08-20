# macOS login startup with launchd and tmux

This setup starts the proxy after the macOS user logs in. It creates a detached tmux session named `LLM` and runs:

```bash
.venv/bin/python -u start_proxy.py --ui
```

The repository-owned launcher is `scripts/start_proxy_tmux.sh`. It:

- resolves the repository from its own location, so `.env`, `upstreams.json`, and `.codex_oauth.json` are read from the correct working directory;
- uses the repository's `.venv` rather than the macOS system Python;
- exits successfully when the exact tmux session `LLM` already exists;
- supports Homebrew tmux on both Apple Silicon and Intel Macs; and
- keeps console output visible in tmux while also appending it to `logs/proxy-console.log`.

If Codex access-token refresh fails at login because the network or proxy is not ready yet, `start_proxy.py` retries for **180 seconds** by default, every **15 seconds** (log prefix `[codex-auth]`). Override with:

- `CODEX_STARTUP_RETRY_SECONDS` (default `180`; `0` = single attempt)
- `CODEX_STARTUP_RETRY_INTERVAL_SECONDS` (default `15`)

Missing enabled accounts or a missing `refresh_token` fails immediately without waiting out the retry window.

This is a login-time LaunchAgent, not a pre-login system daemon. A per-user LaunchAgent is the appropriate choice because tmux, the repository, environment variables, and Codex OAuth state all belong to the user account.

## Prerequisites

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage_codex_accounts.py list
```

Install tmux if necessary:

```bash
brew install tmux
```

## Install the LaunchAgent

Create `~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist`. Replace both `/absolute/path/to/LLM_PROXY` values with the absolute path to this checkout; launchd does not expand `~`.

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

Validate and load it:

```bash
chmod +x scripts/start_proxy_tmux.sh
plutil -lint ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl kickstart -k "gui/$(id -u)/io.github.mrchen116.llm-bridge"
```

If the service was already loaded and the plist changed, unload it before loading the new version:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
```

## Verify and operate

```bash
tmux list-sessions
tmux attach -t LLM
curl -fsS http://127.0.0.1:4000/health
```

Detach from tmux with `Ctrl-b d`. Running `exit` inside the pane stops the proxy and closes the session.

After changing `.env`, `upstreams.json`, or OAuth account configuration, restart the session so the proxy reloads them:

```bash
tmux kill-session -t LLM
launchctl kickstart -k "gui/$(id -u)/io.github.mrchen116.llm-bridge"
```

To inspect recent output without attaching:

```bash
tmux capture-pane -p -t LLM:0.0 -S -100
tail -f logs/proxy-console.log
```

## Disable

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.github.mrchen116.llm-bridge.plist
tmux kill-session -t LLM
```

Do not add `KeepAlive=true` to this LaunchAgent. The detached tmux client exits as soon as it creates the session, so launchd would repeatedly rerun the launcher. If crash recovery is required, let launchd run Python directly in the foreground with `KeepAlive`, rather than combining `KeepAlive` with detached tmux.

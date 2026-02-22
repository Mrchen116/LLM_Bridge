import json
from pathlib import Path

from fastapi.testclient import TestClient


def _write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def test_session_inspector_disabled_returns_404(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "false")
    resp = client.get("/api/session-inspector/sessions")
    assert resp.status_code == 404


def test_session_inspector_sessions_and_timeline(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    resp_ui = client.get("/ui/session-inspector")
    assert resp_ui.status_code == 200

    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-34-56_789_demo-session"
    _write_json(
        session_dir / "2026-02-22_12-34-56_789-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "请列出当前目录"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "Run shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"cmd": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-34-56_789-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Bash",
                                    "arguments": '{"cmd":"ls"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-34-58_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "执行完了告诉我"}],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-34-58_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_2",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "已完成。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        },
    )

    resp_sessions = client.get("/api/session-inspector/sessions", params={"q": "demo-session"})
    assert resp_sessions.status_code == 200
    sessions_payload = resp_sessions.json()
    assert sessions_payload["items"]
    assert sessions_payload["items"][0]["session_id"] == "demo-session"

    resp_timeline = client.get(
        "/api/session-inspector/sessions/demo-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp_timeline.status_code == 200
    timeline_payload = resp_timeline.json()
    assert timeline_payload["stats"]["total_events"] >= 3
    assert timeline_payload["stats"]["tool_events"] >= 1

    tool_events = [ev for ev in timeline_payload["events"] if ev["kind"] == "tool_call"]
    assert tool_events
    assert tool_events[0]["tool_name"] == "Bash"
    assert isinstance(tool_events[0]["tool_args"], dict)
    assert tool_events[0]["tool_def"]["name"] == "Bash"

    resp_tool_only = client.get(
        "/api/session-inspector/sessions/demo-session/timeline",
        params={"include_non_tool": "false"},
    )
    assert resp_tool_only.status_code == 200
    tool_only_payload = resp_tool_only.json()
    assert tool_only_payload["events"]
    assert all(ev["kind"] == "tool_call" for ev in tool_only_payload["events"])

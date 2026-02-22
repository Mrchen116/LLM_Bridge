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
    assert tool_events[0]["source_files"]["request"].endswith(
        "2026-02-22_12-34-56_789-req-openai_chat.json"
    )
    assert tool_events[0]["source_files"]["response"].endswith(
        "2026-02-22_12-34-56_789-non-stream-res-openai_chat.json"
    )

    resp_tool_only = client.get(
        "/api/session-inspector/sessions/demo-session/timeline",
        params={"include_non_tool": "false"},
    )
    assert resp_tool_only.status_code == 200
    tool_only_payload = resp_tool_only.json()
    assert tool_only_payload["events"]
    assert all(ev["kind"] == "tool_call" for ev in tool_only_payload["events"])


def test_session_inspector_lane_grouping_uses_full_context(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")

    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-40-00_000_grouping-session"

    # turn-1 and turn-2 share the same canonical prefix context:
    # same instructions + same tools + same tool_choice.
    _write_json(
        session_dir / "2026-02-22_12-40-00_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "instructions": "system A",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-40-00_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_a1",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok-1"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-40-00_002-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "instructions": "system A",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-40-00_002-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_a2",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok-2"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    # turn-3 changes tools -> must become a new lane.
    _write_json(
        session_dir / "2026-02-22_12-40-00_003-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "instructions": "system A",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "function", "function": {"name": "Read", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-40-00_003-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_b1",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok-3"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    # turn-4 keeps static config of turn-1/2 but with a longer conversation prefix.
    # It should still map to the same lane by prefix-chain matching.
    _write_json(
        session_dir / "2026-02-22_12-40-00_004-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "instructions": "system A",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "ok-1"},
                {"role": "user", "content": "next"},
                {"role": "assistant", "content": "ok-2"},
                {"role": "user", "content": "more"},
            ],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-40-00_004-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_a3",
            "object": "chat.completion",
            "model": "gpt-5.2-codex",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok-4"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )

    resp_timeline = client.get(
        "/api/session-inspector/sessions/grouping-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp_timeline.status_code == 200
    payload = resp_timeline.json()
    assert payload["stats"]["lane_count"] == 2

    lane_by_turn = {}
    for ev in payload["events"]:
        lane_by_turn.setdefault(ev["turn_ts"], set()).add(ev["lane_id"])

    assert len(lane_by_turn["2026-02-22_12-40-00_001"]) == 1
    assert len(lane_by_turn["2026-02-22_12-40-00_002"]) == 1
    assert len(lane_by_turn["2026-02-22_12-40-00_003"]) == 1
    assert len(lane_by_turn["2026-02-22_12-40-00_004"]) == 1

    lane_1 = next(iter(lane_by_turn["2026-02-22_12-40-00_001"]))
    lane_2 = next(iter(lane_by_turn["2026-02-22_12-40-00_002"]))
    lane_3 = next(iter(lane_by_turn["2026-02-22_12-40-00_003"]))
    lane_4 = next(iter(lane_by_turn["2026-02-22_12-40-00_004"]))

    assert lane_1 == lane_2
    assert lane_1 == lane_4
    assert lane_3 != lane_1


def test_session_inspector_request_summary_uses_last_user_payload(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-41-00_000_summary-session"

    _write_json(
        session_dir / "2026-02-22_12-41-00_001-req-anthropic_messages.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "initial question"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "Task", "input": {"x": 1}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": [{"type": "text", "text": "LAST_USER_TOOL_RESULT_TEXT"}],
                        }
                    ],
                },
            ],
            "system": [{"type": "text", "text": "system"}],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-41-00_001-non-stream-res-anthropic_messages.json",
        {
            "id": "dummy",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        },
    )

    resp_timeline = client.get(
        "/api/session-inspector/sessions/summary-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp_timeline.status_code == 200
    payload = resp_timeline.json()
    request_events = [ev for ev in payload["events"] if ev["event_id"].endswith(":request:0")]
    assert request_events
    assert "LAST_USER_TOOL_RESULT_TEXT" in request_events[0]["summary"]

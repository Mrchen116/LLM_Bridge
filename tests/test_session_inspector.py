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


def test_session_inspector_log_file_endpoint(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-35-00_000_file-view-session"
    log_path = session_dir / "2026-02-22_12-35-00_000-downstream-res-openai_chat.json"
    raw_content = '{"ok": true, "text": "line1\\nline2"}\n'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(raw_content, encoding="utf-8")

    resp = client.get(
        "/api/session-inspector/log-file",
        params={"path": "logs/session/2026-02-22_12-35-00_000_file-view-session/2026-02-22_12-35-00_000-downstream-res-openai_chat.json"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["path"].endswith("2026-02-22_12-35-00_000-downstream-res-openai_chat.json")
    assert payload["content"] == raw_content
    assert payload["truncated"] is False

    blocked = client.get("/api/session-inspector/log-file", params={"path": "../README.md"})
    assert blocked.status_code == 400


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


def test_session_inspector_request_event_uses_tool_result_when_last_role_is_tool(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-42-00_000_tool-result-session"

    _write_json(
        session_dir / "2026-02-22_12-42-00_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [
                {"role": "user", "content": "开个subagent，让它给你讲个冷笑话"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "task", "arguments": '{"prompt":"tell joke"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"name":"task","output":{"status":"completed","message":"joke"}}',
                },
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-42-00_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_tool_result",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "冷笑话：..."},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/tool-result-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    request_events = [ev for ev in payload["events"] if ev["event_id"].endswith(":request:0")]
    assert request_events
    assert request_events[0]["kind"] == "tool_result"
    assert '"name":"task"' in request_events[0]["summary"]


def test_session_inspector_request_event_prefers_latest_function_call_output_for_responses(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-42-30_000_responses-tool-output-session"

    _write_json(
        session_dir / "2026-02-22_12-42-30_001-req-openai_responses.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "EARLY_USER_QUESTION"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "我先执行命令"}]},
                {
                    "type": "function_call",
                    "call_id": "call_rsp_1",
                    "name": "exec_command",
                    "arguments": '{"cmd":"echo hi"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_rsp_1",
                    "output": "LATEST_FUNCTION_CALL_OUTPUT_TEXT",
                },
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-42-30_001-non-stream-res-openai_responses.json",
        {
            "id": "resp_tool_out_1",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/responses-tool-output-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    request_events = [ev for ev in payload["events"] if ev["event_id"].endswith(":request:0")]
    assert request_events
    assert request_events[0]["kind"] == "tool_result"
    assert "LATEST_FUNCTION_CALL_OUTPUT_TEXT" in request_events[0]["summary"]


def test_session_inspector_request_events_include_contiguous_tail_tool_results_openai_chat(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-42-40_000_multi-tool-tail-chat-session"

    _write_json(
        session_dir / "2026-02-22_12-42-40_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [
                {"role": "user", "content": "OLDER_USER_TEXT"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_chat_1",
                            "type": "function",
                            "function": {"name": "exec_command", "arguments": '{"cmd":"echo 1"}'},
                        },
                        {
                            "id": "call_chat_2",
                            "type": "function",
                            "function": {"name": "exec_command", "arguments": '{"cmd":"echo 2"}'},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_chat_1", "content": "CHAT_TOOL_OUTPUT_1"},
                {"role": "tool", "tool_call_id": "call_chat_2", "content": "CHAT_TOOL_OUTPUT_2"},
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-42-40_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_multi_tool_tail",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/multi-tool-tail-chat-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    request_events = [ev for ev in payload["events"] if ":request:" in ev["event_id"]]
    assert len(request_events) == 2
    assert request_events[0]["kind"] == "tool_result"
    assert "CHAT_TOOL_OUTPUT_1" in request_events[0]["summary"]
    assert request_events[1]["kind"] == "tool_result"
    assert "CHAT_TOOL_OUTPUT_2" in request_events[1]["summary"]


def test_session_inspector_request_events_include_contiguous_tail_tool_results_openai_responses(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-42-50_000_multi-tool-tail-responses-session"

    _write_json(
        session_dir / "2026-02-22_12-42-50_001-req-openai_responses.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "OLDER_USER_TEXT"}]},
                {
                    "type": "function_call",
                    "call_id": "call_rsp_1",
                    "name": "exec_command",
                    "arguments": '{"cmd":"echo 1"}',
                },
                {"type": "function_call_output", "call_id": "call_rsp_1", "output": "RSP_TOOL_OUTPUT_1"},
                {"type": "function_call_output", "call_id": "call_rsp_2", "output": "RSP_TOOL_OUTPUT_2"},
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-42-50_001-non-stream-res-openai_responses.json",
        {
            "id": "resp_multi_tool_tail",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/multi-tool-tail-responses-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    request_events = [ev for ev in payload["events"] if ":request:" in ev["event_id"]]
    assert len(request_events) == 2
    assert request_events[0]["kind"] == "tool_result"
    assert "RSP_TOOL_OUTPUT_1" in request_events[0]["summary"]
    assert request_events[1]["kind"] == "tool_result"
    assert "RSP_TOOL_OUTPUT_2" in request_events[1]["summary"]


def test_session_inspector_request_event_treats_responses_developer_as_user_input(
    client: TestClient, monkeypatch
):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-42-55_000_developer-tail-responses-session"

    _write_json(
        session_dir / "2026-02-22_12-42-55_001-req-openai_responses.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "input": [
                {"role": "developer", "content": [{"type": "input_text", "text": "DEVELOPER_TAIL_TEXT"}]}
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-42-55_001-non-stream-res-openai_responses.json",
        {
            "id": "resp_developer_tail",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/developer-tail-responses-session/timeline",
        params={"include_non_tool": "true"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    request_events = [ev for ev in payload["events"] if ":request:" in ev["event_id"]]
    assert len(request_events) == 1
    assert request_events[0]["kind"] == "user_input"
    assert "DEVELOPER_TAIL_TEXT" in request_events[0]["summary"]


def test_session_inspector_filtered_scope_stats_follow_keyword_turns(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-43-00_000_stats-scope-session"

    _write_json(
        session_dir / "2026-02-22_12-43-00_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "newtopic: run bash"}],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-43-00_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_scope_1",
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
                                "id": "call_scope_1",
                                "type": "function",
                                "function": {"name": "Bash", "arguments": '{"cmd":"ls"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        },
    )

    _write_json(
        session_dir / "2026-02-22_12-43-00_002-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "other topic: run read"}],
            "tools": [{"type": "function", "function": {"name": "Read", "parameters": {}}}],
            "tool_choice": "auto",
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-43-00_002-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_scope_2",
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
                                "id": "call_scope_2",
                                "type": "function",
                                "function": {"name": "Read", "arguments": '{"path":"README.md"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/stats-scope-session/timeline",
        params={"include_non_tool": "true", "q": "newtopic"},
    )
    assert resp.status_code == 200
    payload = resp.json()

    filtered_scope = payload["stats"]["filtered_scope"]
    assert filtered_scope["turn_count_after_keywords"] == 1
    assert filtered_scope["session_tokens"]["input_tokens"] == 11
    assert filtered_scope["session_tokens"]["output_tokens"] == 3
    assert filtered_scope["session_tokens"]["num_turns"] == 1

    assert filtered_scope["tool_calls"]["total_calls"] == 1
    assert filtered_scope["tool_calls"]["by_tool"] == [{"tool_name": "Bash", "count": 1}]

    assert len(filtered_scope["agents"]) == 1
    agent_stats = filtered_scope["agents"][0]
    assert agent_stats["tokens"]["input_tokens"] == 11
    assert agent_stats["tokens"]["output_tokens"] == 3
    assert agent_stats["tokens"]["num_turns"] == 1
    assert agent_stats["tool_calls_total"] == 1
    assert agent_stats["tool_calls_by_name"] == [{"tool_name": "Bash", "count": 1}]


def test_session_inspector_keyword_filter_applies_to_whole_turn(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-50-00_000_turn-filter-session"

    _write_json(
        session_dir / "2026-02-22_12-50-00_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [
                {"role": "user", "content": "普通问题"},
                {"role": "user", "content": "这里包含 KeepTurn 关键词"},
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-50-00_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_keep",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "响应文本不含关键词"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    _write_json(
        session_dir / "2026-02-22_12-50-00_002-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "另一个请求"}],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-50-00_002-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_drop",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "普通响应"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/turn-filter-session/timeline",
        params={"include_non_tool": "true", "q": "KeepTurn"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["events"]
    assert {ev["turn_ts"] for ev in payload["events"]} == {"2026-02-22_12-50-00_001"}
    assert any(ev["kind"] == "user_input" for ev in payload["events"])
    assert any(ev["kind"] == "assistant_text" for ev in payload["events"])


def test_session_inspector_negative_keyword_removes_whole_turn(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    session_dir = Path.cwd() / "logs" / "session" / "2026-02-22_12-51-00_000_turn-exclude-session"

    _write_json(
        session_dir / "2026-02-22_12-51-00_001-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [
                {"role": "user", "content": "第一段普通输入"},
                {"role": "user", "content": "第二段包含 BlockMe 词"},
            ],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-51-00_001-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_bad",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "这个响应也应被整轮剔除"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    _write_json(
        session_dir / "2026-02-22_12-51-00_002-req-openai_chat.json",
        {
            "_upstream_provider": "codex_oauth",
            "model": "codexOAuth:gpt-5.2-codex",
            "messages": [{"role": "user", "content": "干净请求"}],
        },
    )
    _write_json(
        session_dir / "2026-02-22_12-51-00_002-non-stream-res-openai_chat.json",
        {
            "id": "chatcmpl_good",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "保留响应"},
                    "finish_reason": "stop",
                }
            ],
        },
    )

    resp = client.get(
        "/api/session-inspector/sessions/turn-exclude-session/timeline",
        params={"include_non_tool": "true", "q_not": "BlockMe"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["events"]
    assert {ev["turn_ts"] for ev in payload["events"]} == {"2026-02-22_12-51-00_002"}


def test_session_inspector_keyword_presets_api(client: TestClient, monkeypatch):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")

    resp_get_empty = client.get("/api/session-inspector/keyword-presets")
    assert resp_get_empty.status_code == 200
    assert resp_get_empty.json()["presets"] == []

    payload = {
        "default_preset_id": "p1",
        "presets": [
            {
                "id": "p1",
                "name": "默认",
                "include_keywords": ["foo", "foo", " bar "],
                "exclude_keywords": ["bad"],
            }
        ],
    }
    resp_put = client.put("/api/session-inspector/keyword-presets", json=payload)
    assert resp_put.status_code == 200
    body = resp_put.json()
    assert body["default_preset_id"] == "p1"
    assert body["presets"][0]["include_keywords"] == ["foo", "bar"]
    assert body["presets"][0]["exclude_keywords"] == ["bad"]

    resp_get = client.get("/api/session-inspector/keyword-presets")
    assert resp_get.status_code == 200
    assert resp_get.json()["default_preset_id"] == "p1"

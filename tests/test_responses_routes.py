import httpx
import json
from pathlib import Path
from fastapi.testclient import TestClient

from tests.support import FakeAsyncClient


def test_responses_profile_unsupported_for_protocol(client: TestClient):
    """测试 profile 不支持 responses 协议时返回 unsupported 错误。"""
    payload = {"model": "chatOnly:chat-only-model", "input": "hello"}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "unsupported_for_upstream"


def test_openai_responses_codex_oauth_non_stream_passthrough(client: TestClient):
    """测试 /v1/responses 在 codex_oauth 下非流式透传且强制 store=false。"""
    upstream_body = {
        "id": "resp_passthrough_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json=upstream_body,
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "store": True}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert resp.json() == upstream_body
    assert FakeAsyncClient.last_post_args["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeAsyncClient.last_post_args["json"]["store"] is False


def test_responses_with_session_writes_raw_and_session_logs(client_with_logs: TestClient):
    """测试 responses 带 session 时同时写 raw(5) 与 session(3) 日志。"""
    upstream_body = {
        "id": "resp_session_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json=upstream_body,
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": False}
    session_id = "responses-session-logs"
    resp = client_with_logs.post("/v1/responses", json=payload, headers={"X-Session-Id": session_id})
    assert resp.status_code == 200

    raw_dir = Path.cwd() / "logs" / "raw" / "openai_codex"
    assert sorted(raw_dir.glob("*-req-openai_responses.json"))
    assert sorted(raw_dir.glob("*-upstream-req-openai_responses.json"))
    assert sorted(raw_dir.glob("*-headers-openai_responses.json"))
    assert sorted(raw_dir.glob("*-upstream-res-openai_responses.json"))
    assert sorted(raw_dir.glob("*-downstream-res-openai_responses.json"))

    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob(f"*_{session_id}"))
    assert session_dirs
    session_dir = session_dirs[-1]
    req_files = sorted(session_dir.glob("*-req-openai_responses.json"))
    down_files = sorted(session_dir.glob("*-downstream-res-openai_responses.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-openai_responses.json"))
    assert req_files and down_files and non_stream_files

    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        obj = json.load(f)
    assert obj["usage"]["input_tokens"] == 3
    assert obj["usage"]["output_tokens"] == 2

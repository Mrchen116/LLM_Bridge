import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as app_module
import token_auth


TEST_UPSTREAM_CONFIG: Dict[str, Any] = {
    "defaultProfile": "moonshot",
    "profiles": {
        "moonshot": {
            "provider": "openai_compatible",
            "baseUrl": "https://mock.openai.local/v1",
            "auth": {"apiKeyEnv": "MOONSHOT_API_KEY"},
            "capabilities": {"ingress": ["anthropic_messages", "openai_chat"]},
            "defaults": {"model": "kimi-k2.5", "timeoutSeconds": 5, "sslVerify": True, "retryMax": 1},
        },
        "moonshotAnthropic": {
            "provider": "anthropic",
            "baseUrl": "https://mock.anthropic.local/v1",
            "auth": {"apiKeyEnv": "MOONSHOT_API_KEY"},
            "capabilities": {"ingress": ["anthropic_messages"]},
            "defaults": {"model": "claude-test", "timeoutSeconds": 5, "sslVerify": True, "retryMax": 1},
        },
        "chatOnly": {
            "provider": "openai_compatible",
            "baseUrl": "https://mock.chatonly.local/v1",
            "auth": {"apiKeyEnv": "MOONSHOT_API_KEY"},
            "capabilities": {"ingress": ["openai_chat"]},
            "defaults": {"model": "chat-only-model", "timeoutSeconds": 5, "sslVerify": True, "retryMax": 1},
        },
        "codexOAuth": {
            "provider": "codex_oauth",
            "baseUrl": "https://api.openai.com/v1",
            "auth": {"codexEndpoint": "https://chatgpt.com/backend-api/codex/responses"},
            "capabilities": {"ingress": ["anthropic_messages", "openai_chat", "openai_responses"]},
            "defaults": {"model": "gpt-5.2-codex", "timeoutSeconds": 5, "sslVerify": True, "retryMax": 1},
        },
    },
}


class FakeStreamResponse:
    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[Dict[str, str]] = None,
        lines: Optional[List[str]] = None,
        raw_chunks: Optional[List[bytes]] = None,
        read_bytes: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}
        self._lines = lines or []
        self._raw_chunks = raw_chunks or []
        self._read_bytes = read_bytes

    async def aread(self) -> bytes:
        return self._read_bytes

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_raw(self):
        for chunk in self._raw_chunks:
            yield chunk


class FakeStreamContext:
    def __init__(self, response: FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> FakeStreamResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeAsyncClient:
    post_response: httpx.Response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"ok": True},
    )
    stream_response: FakeStreamResponse = FakeStreamResponse()
    last_post_args: Dict[str, Any] = {}
    last_stream_args: Dict[str, Any] = {}

    def __init__(self, *args, **kwargs) -> None:
        self._args = args
        self._kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, headers: Dict[str, str], json: Dict[str, Any]) -> httpx.Response:
        FakeAsyncClient.last_post_args = {"url": url, "headers": headers, "json": json}
        return FakeAsyncClient.post_response

    def stream(self, method: str, url: str, headers: Dict[str, str], json: Dict[str, Any]) -> FakeStreamContext:
        FakeAsyncClient.last_stream_args = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
        }
        return FakeStreamContext(FakeAsyncClient.stream_response)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "codex-access-token")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "org-test-account")
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module, "_dump_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)


@pytest.fixture()
def client_with_logs(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "codex-access-token")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "org-test-account")
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)


def test_health_ok(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_messages_openai_non_stream_success(client: TestClient):
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        },
    )
    payload = {
        "model": "moonshot:kimi-k2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32,
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["usage"]["input_tokens"] == 3
    assert body["usage"]["output_tokens"] == 4


def test_messages_anthropic_non_stream_passthrough(client: TestClient):
    upstream_body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "pong"}],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json=upstream_body,
    )
    payload = {
        "model": "moonshotAnthropic:claude-test",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 200
    assert resp.json() == upstream_body


def test_messages_stream_anthropic_passthrough(client: TestClient):
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        raw_chunks=[b"data: ping\n\n", b"data: [DONE]\n\n"],
    )
    payload = {
        "model": "moonshotAnthropic:claude-test",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/messages", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "data: ping" in data


def test_messages_stream_disabled(client: TestClient, monkeypatch):
    monkeypatch.setattr(app_module, "BAN_STREAM", True)
    payload = {
        "model": "moonshot:kimi-k2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "stream_disabled"


def test_messages_profile_unsupported_for_protocol(client: TestClient):
    payload = {
        "model": "chatOnly:chat-only-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "unsupported_for_upstream"


def test_responses_profile_unsupported_for_protocol(client: TestClient):
    payload = {"model": "chatOnly:chat-only-model", "input": "hello"}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "unsupported_for_upstream"


def test_chat_completions_non_stream_passthrough(client_with_logs: TestClient):
    upstream_body = {
        "id": "chatcmpl_1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json=upstream_body,
    )
    payload = {"model": "moonshot:kimi-k2.5", "messages": [{"role": "user", "content": "hello"}]}
    resp = client_with_logs.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json() == upstream_body

    log_dir = Path.cwd() / "logs" / "openai"
    assert log_dir.exists()
    req_files = sorted(log_dir.glob("*-req.json"))
    res_files = sorted(log_dir.glob("*--res.json"))
    assert req_files, "应生成 openai 请求日志"
    assert res_files, "应生成 openai 响应日志"


def test_chat_completions_stream_passthrough(client: TestClient):
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"id":"chatcmpl_1","choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "moonshot:kimi-k2.5",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "data: [DONE]" in data


def test_chat_completions_codex_oauth_uses_codex_endpoint_and_headers(client: TestClient):
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "resp_1",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)

    assert resp.status_code == 200
    assert FakeAsyncClient.last_post_args["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeAsyncClient.last_post_args["headers"]["Authorization"] == "Bearer codex-access-token"
    assert FakeAsyncClient.last_post_args["headers"]["ChatGPT-Account-Id"] == "org-test-account"
    assert FakeAsyncClient.last_post_args["json"]["instructions"] == "You are a helpful assistant."
    assert FakeAsyncClient.last_post_args["json"]["input"][0]["role"] == "user"
    assert FakeAsyncClient.last_post_args["json"]["input"][0]["content"][0]["text"] == "hello"
    assert resp.json()["choices"][0]["message"]["content"] == "ok"


def test_chat_completions_codex_oauth_mapping_matches_opencode_style(client: TestClient):
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "resp_map_1",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 6, "output_tokens": 2},
        },
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [
            {"role": "system", "content": "你是系统提示"},
            {"role": "developer", "content": "你是开发者提示"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "正在调用工具",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "tool_a", "arguments": "{\"x\":1}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
        ],
        "max_tokens": 12,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "tool_a",
                    "description": "demo",
                    "parameters": {"type": "object", "properties": {"x": {"type": "number"}}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "tool_a"}},
    }

    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    up = FakeAsyncClient.last_post_args["json"]
    assert up["max_output_tokens"] == 12
    assert up["instructions"] == "You are a helpful assistant."
    assert up["input"][0]["role"] == "system"
    assert up["input"][0]["content"] == "你是系统提示"
    assert up["input"][1]["role"] == "developer"
    assert up["input"][1]["content"] == "你是开发者提示"
    assert any(isinstance(item, dict) and item.get("type") == "function_call" for item in up["input"])
    assert any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in up["input"])
    assert up["tool_choice"]["name"] == "tool_a"
    assert up["tools"][0]["type"] == "function"
    assert up["tools"][0]["name"] == "tool_a"


def test_chat_completions_codex_oauth_stream_bridge(client: TestClient):
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "resp_stream_1",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "pong"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        },
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "只回复 pong"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "pong" in data
    assert "data: [DONE]" in data


def test_openai_responses_codex_oauth_non_stream_passthrough(client: TestClient):
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


def test_codex_oauth_login_callback_and_upstream_use_store_token(client: TestClient, monkeypatch):
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    async def fake_exchange_code_for_tokens(code: str, redirect_uri: str, code_verifier: str) -> Dict[str, Any]:
        return {
            "access_token": "oauth-access-from-callback",
            "refresh_token": "oauth-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(token_auth, "_exchange_code_for_tokens", fake_exchange_code_for_tokens)

    login_resp = client.get("/auth/codex/login")
    assert login_resp.status_code == 200
    assert login_resp.json()["ok"] is True
    state = login_resp.json()["state"]
    assert "auth.openai.com/oauth/authorize" in login_resp.json()["authorization_url"]

    callback_resp = client.get("/auth/codex/callback", params={"code": "demo-code", "state": state})
    assert callback_resp.status_code == 200
    assert "登录成功" in callback_resp.text

    status_resp = client.get("/auth/codex/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["authorized"] is True
    assert status_resp.json()["source"] == "store"

    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "resp_2",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    upstream_resp = client.post("/v1/chat/completions", json=payload)
    assert upstream_resp.status_code == 200
    assert FakeAsyncClient.last_post_args["headers"]["Authorization"] == "Bearer oauth-access-from-callback"


def test_codex_oauth_auto_refresh_on_upstream_call(client: TestClient, monkeypatch):
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    async def fake_refresh_access_token(refresh_token: str) -> Dict[str, Any]:
        return {
            "access_token": "refreshed-access",
            "refresh_token": "refresh-new",
            "expires_in": 3600,
        }

    monkeypatch.setattr(token_auth, "_refresh_access_token", fake_refresh_access_token)

    expired = {
        "codex_oauth": {
            "access_token": "expired-token",
            "refresh_token": "refresh-old",
            "expires_at": 1,
            "account_id": "",
            "updated_at": 1,
        }
    }
    Path(".codex_oauth.json").write_text(json.dumps(expired), encoding="utf-8")
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "id": "resp_3",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_post_args["headers"]["Authorization"] == "Bearer refreshed-access"

    store = json.loads(Path(".codex_oauth.json").read_text(encoding="utf-8"))
    assert store["codex_oauth"]["refresh_token"] == "refresh-new"


def test_count_tokens_invalid_json(client: TestClient):
    resp = client.post(
        "/v1/messages/count_tokens",
        content="not-json",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_session_stats_not_found(client: TestClient):
    resp = client.get("/session/not-exist/stats")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_messages_stream_writes_session_logs(client_with_logs: TestClient):
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"id":"chatcmpl_1","model":"kimi-k2.5","choices":[{"delta":{"content":"hello"},"finish_reason":null}],"usage":{"prompt_tokens":2,"completion_tokens":1}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "moonshot:kimi-k2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "metadata": {"user_id": "session_streamcase"},
    }
    with client_with_logs.stream("POST", "/v1/messages", json=payload) as resp:
        stream_data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "event: message_start" in stream_data
    assert "event: content_block_delta" in stream_data
    assert "event: message_stop" in stream_data

    session_root = Path.cwd() / "logs" / "session"
    assert session_root.exists()
    session_dirs = sorted(session_root.glob("*_streamcase"))
    assert session_dirs, "应为该 session 生成目录"
    session_dir = session_dirs[-1]

    req_files = sorted(session_dir.glob("*-req.json"))
    down_files = sorted(session_dir.glob("*-downstream-res.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res.json"))

    assert req_files, "应生成 session 请求日志"
    assert down_files, "应生成 session 流式响应日志"
    assert non_stream_files, "应生成 session 非流式聚合日志"

    with req_files[-1].open("r", encoding="utf-8") as f:
        req_obj = json.load(f)
    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        non_stream_obj = json.load(f)

    assert req_obj["model"] == "moonshot:kimi-k2.5"
    assert non_stream_obj["type"] == "message"
    assert non_stream_obj["usage"]["input_tokens"] == 2
    assert non_stream_obj["usage"]["output_tokens"] == 1

    anthropic_log_dir = Path.cwd() / "logs" / "anthropic"
    assert anthropic_log_dir.exists()
    assert sorted(anthropic_log_dir.glob("*-req.json")), "应生成 anthropic 请求日志"
    assert sorted(anthropic_log_dir.glob("*-headers.json")), "应生成 anthropic 请求头日志"
    assert sorted(anthropic_log_dir.glob("*-upstream-res.json")), "应生成 anthropic 上游响应日志"
    assert sorted(anthropic_log_dir.glob("*-downstream-res.json")), "应生成 anthropic 下游响应日志"


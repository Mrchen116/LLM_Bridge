import json
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as app_module
from tests.support import ConnectErrorAsyncClient, FakeStreamResponse, TEST_UPSTREAM_CONFIG


def test_messages_openai_non_stream_success(client: TestClient):
    """测试 /v1/messages 桥接到 OpenAI 兼容上游的非流式成功路径。"""
    from tests.support import FakeAsyncClient

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
    """测试 /v1/messages 在 anthropic provider 下非流式透传。"""
    from tests.support import FakeAsyncClient

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


def test_messages_codex_oauth_non_stream_bridge(client: TestClient):
    """测试 /v1/messages 在 codex_oauth 下非流式桥接到 responses。"""
    from tests.support import FakeAsyncClient

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_msg_1","output":[{"type":"message","content":[{"type":"output_text","text":"答案：x=7,y=3"}]}],"usage":{"input_tokens":5,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "解方程"}],
        "max_tokens": 128,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"][0]["type"] == "text"
    assert "x=7" in body["content"][0]["text"]

    up = FakeAsyncClient.last_stream_args["json"]
    assert up["model"] == "gpt-5.2-codex"
    assert up["instructions"] == "You are a helpful assistant."
    assert up["input"][0]["role"] == "user"
    assert up["store"] is False
    assert "reasoning" not in up
    assert "reasoning.encrypted_content" in up["include"]


def test_messages_codex_oauth_model_suffix_sets_reasoning_effort(client: TestClient):
    """测试 /v1/messages 在 codex_oauth 下支持模型名 @high 后缀。"""
    from tests.support import FakeAsyncClient

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_msg_suffix_1","output":[{"type":"message","content":[{"type":"output_text","text":"答案"}]}],"usage":{"input_tokens":5,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex@high",
        "messages": [{"role": "user", "content": "解方程"}],
        "max_tokens": 128,
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 200

    up = FakeAsyncClient.last_stream_args["json"]
    assert up["model"] == "gpt-5.2-codex"
    assert up["reasoning"]["effort"] == "high"


def test_messages_stream_anthropic_passthrough(client: TestClient):
    """测试 /v1/messages 在 anthropic provider 下流式透传。"""
    from tests.support import FakeAsyncClient

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


def test_messages_codex_oauth_stream_bridge(client: TestClient):
    """测试 /v1/messages 在 codex_oauth 下流式桥接输出。"""
    from tests.support import FakeAsyncClient

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.output_text.delta","delta":"x=7"}',
            'data: {"type":"response.output_text.delta","delta":",y=3"}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "解方程"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/messages", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "event: message_start" in data
    assert "event: content_block_delta" in data
    assert "x=7,y=3" in data
    assert "event: message_stop" in data


def test_messages_codex_oauth_stream_bridge_maps_function_call_to_tool_use(client: TestClient):
    """测试 codex function_call 在消息流中被映射为 anthropic tool_use。"""
    from tests.support import FakeAsyncClient

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_tool_1","output":[{"type":"function_call","call_id":"call_123","name":"Task","arguments":"{\\"description\\":\\"Create sonnet file\\",\\"prompt\\":\\"Create empty file\\",\\"subagent_type\\":\\"general-purpose\\",\\"model\\":\\"sonnet\\"}"}],"usage":{"input_tokens":9,"output_tokens":5}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "调用工具"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/messages", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "event: content_block_start" in data
    assert '"type": "tool_use"' in data
    assert '"id": "call_123"' in data
    assert '"name": "Task"' in data
    assert '"type": "input_json_delta"' in data
    assert "Create sonnet file" in data
    assert '"stop_reason": "tool_use"' in data
    assert "event: message_stop" in data


def test_messages_codex_oauth_non_stream_connect_error_returns_502(tmp_path, monkeypatch):
    """测试 codex 上游连接异常时 /v1/messages 非流式返回 502。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_label": "primary",
                "accounts": [
                    {
                        "label": "primary",
                        "account_id": "org-test-account",
                        "priority": 100,
                        "enabled": True,
                        "access_token": "codex-access-token",
                        "refresh_token": "codex-refresh-token",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module, "_dump_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", ConnectErrorAsyncClient)
    local_client = TestClient(app_module.app)

    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    resp = local_client.post("/v1/messages", json=payload)
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["type"] == "upstream_connection_error"


def test_messages_codex_oauth_stream_connect_error_emits_error_event(tmp_path, monkeypatch):
    """测试 codex 上游连接异常时 /v1/messages 流式返回 error 事件。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_label": "primary",
                "accounts": [
                    {
                        "label": "primary",
                        "account_id": "org-test-account",
                        "priority": 100,
                        "enabled": True,
                        "access_token": "codex-access-token",
                        "refresh_token": "codex-refresh-token",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module, "_dump_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", ConnectErrorAsyncClient)
    local_client = TestClient(app_module.app)

    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    with local_client.stream("POST", "/v1/messages", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert "event: error" in data
    assert "ConnectError: mock connect failure" in data
    assert "event: message_stop" in data


def test_messages_stream_disabled(client: TestClient, monkeypatch):
    """测试启用 BAN_STREAM 时拒绝 /v1/messages 流式请求。"""
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
    """测试 profile 不支持 messages 协议时返回 unsupported 错误。"""
    payload = {
        "model": "chatOnly:chat-only-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = client.post("/v1/messages", json=payload)
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "unsupported_for_upstream"

import httpx
import json
from pathlib import Path
from fastapi.testclient import TestClient

from tests.support import FakeAsyncClient, FakeStreamResponse


def test_chat_completions_non_stream_passthrough(client_with_logs: TestClient):
    """测试 /v1/chat/completions 非流式透传并落盘日志。"""
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


def test_chat_completions_stream_passthrough(client: TestClient):
    """测试 /v1/chat/completions 流式透传 DONE 结束标记。"""
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
    """测试 codex_oauth 下 chat 接口改写到 codex endpoint 且携带鉴权头。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_1","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":2,"output_tokens":1}}}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)

    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeAsyncClient.last_stream_args["headers"]["authorization"] == "Bearer codex-access-token"
    assert FakeAsyncClient.last_stream_args["headers"]["chatgpt-account-id"] == "org-test-account"
    assert FakeAsyncClient.last_stream_args["headers"]["originator"] == "codex_cli_rs"
    assert FakeAsyncClient.last_stream_args["headers"]["accept"] == "text/event-stream"
    assert FakeAsyncClient.last_stream_args["headers"]["x-codex-beta-features"] == "multi_agent,prevent_idle_sleep"
    assert "session_id" not in FakeAsyncClient.last_stream_args["headers"]
    assert FakeAsyncClient.last_stream_args["json"]["instructions"] == "You are a helpful assistant."
    assert FakeAsyncClient.last_stream_args["json"]["input"][0]["role"] == "user"
    assert FakeAsyncClient.last_stream_args["json"]["input"][0]["content"][0]["text"] == "hello"
    assert FakeAsyncClient.last_stream_args["json"]["stream"] is True
    assert resp.json()["choices"][0]["message"]["content"] == "ok"


def test_chat_completions_codex_oauth_mapping_matches_opencode_style(client: TestClient):
    """测试 chat 请求字段向 codex responses 的映射行为。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_map_1","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":6,"output_tokens":2}}}',
            "data: [DONE]",
        ],
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
    up = FakeAsyncClient.last_stream_args["json"]
    assert "max_tokens" not in up
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
    assert up["reasoning"]["effort"] == "medium"
    assert "reasoning.encrypted_content" in up["include"]


def test_chat_completions_codex_oauth_non_stream_maps_function_call_to_tool_calls(client: TestClient):
    """测试 codex responses 的 function_call 会映射为 chat 的 tool_calls。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            (
                'data: {"type":"response.completed","response":{"id":"resp_fc_1","output":['
                '{"type":"function_call","call_id":"call_1","name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}'
                '],"usage":{"input_tokens":6,"output_tokens":1}}}'
            ),
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "列出当前目录"}],
        "stream": False,
    }
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    obj = resp.json()
    assert obj["choices"][0]["finish_reason"] == "tool_calls"
    assert obj["choices"][0]["message"]["role"] == "assistant"
    assert obj["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"
    assert obj["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"
    assert obj["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'


def test_chat_completions_codex_oauth_stream_emits_tool_calls_chunks(client: TestClient):
    """测试 codex_oauth 流式桥接可返回 tool_calls 并以 tool_calls 结束。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            (
                'data: {"type":"response.completed","response":{"id":"resp_fc_stream_1","output":['
                '{"type":"function_call","call_id":"call_1","name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}'
                '],"usage":{"input_tokens":6,"output_tokens":1}}}'
            ),
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "列出当前目录"}],
        "stream": True,
    }
    with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
        data = "".join(resp.iter_text())
    assert resp.status_code == 200
    assert '"tool_calls"' in data
    assert '"finish_reason": "tool_calls"' in data
    assert "data: [DONE]" in data


def test_chat_completions_codex_oauth_reasoning_effort_and_include_merge(client: TestClient):
    """测试 reasoning_effort 映射及 include 字段并集合并。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_map_2","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":4,"output_tokens":1}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "high",
        "include": ["foo.bar"],
    }

    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    up = FakeAsyncClient.last_stream_args["json"]
    assert up["reasoning"]["effort"] == "high"
    assert "foo.bar" in up["include"]
    assert "reasoning.encrypted_content" in up["include"]


def test_chat_completions_codex_oauth_strip_sampling_params(client: TestClient):
    """测试 codex_oauth 场景下移除不支持的采样参数。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_map_3","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":4,"output_tokens":1}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 1,
        "top_p": 0.9,
    }

    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    up = FakeAsyncClient.last_stream_args["json"]
    assert "temperature" not in up
    assert "top_p" not in up


def test_chat_completions_codex_oauth_stream_bridge(client: TestClient):
    """测试 codex_oauth 下 chat 流式桥接返回文本与 DONE。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.output_text.delta","delta":"pong"}',
            'data: {"type":"response.completed","response":{"id":"resp_stream_1","output":[{"type":"message","content":[{"type":"output_text","text":"pong"}]}],"usage":{"input_tokens":2,"output_tokens":1}}}',
            "data: [DONE]",
        ],
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


def test_chat_completions_codex_oauth_reinjects_encrypted_reasoning_for_trailing_tool_output(client: TestClient):
    """测试 tool output 续轮时也应回填上一轮 encrypted reasoning。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            (
                'data: {"type":"response.completed","response":{"id":"resp_tool_1","output":['
                '{"type":"reasoning","encrypted_content":"enc_tool_1","summary":[]},'
                '{"type":"function_call","call_id":"call_1","name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}'
                '],"usage":{"input_tokens":6,"output_tokens":1}}}'
            ),
            "data: [DONE]",
        ],
    )
    first_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "列出当前目录"}],
        "stream": False,
    }
    resp1 = client.post(
        "/v1/chat/completions",
        json=first_payload,
        headers={"X-Session-Id": "sess_reinject_tool_suffix"},
    )
    assert resp1.status_code == 200

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_tool_2","output":[{"type":"message","content":[{"type":"output_text","text":"done"}]}],"usage":{"input_tokens":8,"output_tokens":2}}}',
            "data: [DONE]",
        ],
    )
    second_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [
            {"role": "user", "content": "列出当前目录"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{\"command\": \"ls\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "a\nb\n"},
        ],
        "stream": False,
    }
    resp2 = client.post(
        "/v1/chat/completions",
        json=second_payload,
        headers={"X-Session-Id": "sess_reinject_tool_suffix"},
    )
    assert resp2.status_code == 200

    up2_input = FakeAsyncClient.last_stream_args["json"]["input"]
    assert any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_tool_1"
        for item in up2_input
    ), "tool output 续轮命中上下文时，应回填上一轮 encrypted_content"


def test_chat_completions_codex_oauth_reinjects_encrypted_reasoning_when_tool_args_json_spacing_differs(client: TestClient):
    """测试 tool args JSON 仅空白差异时，仍应命中回填。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            (
                'data: {"type":"response.completed","response":{"id":"resp_tool_ws_1","output":['
                '{"type":"reasoning","encrypted_content":"enc_tool_ws_1","summary":[]},'
                '{"type":"function_call","call_id":"call_ws_1","name":"bash","arguments":"{\\"command\\":\\"ls\\"}"}'
                '],"usage":{"input_tokens":6,"output_tokens":1}}}'
            ),
            "data: [DONE]",
        ],
    )
    first_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "列出当前目录"}],
        "stream": False,
    }
    resp1 = client.post(
        "/v1/chat/completions",
        json=first_payload,
        headers={"X-Session-Id": "sess_reinject_tool_args_ws"},
    )
    assert resp1.status_code == 200

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_tool_ws_2","output":[{"type":"message","content":[{"type":"output_text","text":"done"}]}],"usage":{"input_tokens":8,"output_tokens":2}}}',
            "data: [DONE]",
        ],
    )
    second_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [
            {"role": "user", "content": "列出当前目录"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_ws_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_ws_1", "content": "a\nb\n"},
        ],
        "stream": False,
    }
    resp2 = client.post(
        "/v1/chat/completions",
        json=second_payload,
        headers={"X-Session-Id": "sess_reinject_tool_args_ws"},
    )
    assert resp2.status_code == 200

    up2_input = FakeAsyncClient.last_stream_args["json"]["input"]
    assert any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_tool_ws_1"
        for item in up2_input
    ), "tool args 的 JSON 空白差异不应导致回填 miss"


def test_chat_completions_with_session_writes_raw_and_session_logs(client_with_logs: TestClient):
    """测试 chat/completions 带 session 时同时写 raw(5) 与 session(3) 日志。"""
    upstream_body = {
        "id": "chatcmpl_session_1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json=upstream_body,
    )

    payload = {"model": "moonshot:kimi-k2.5", "messages": [{"role": "user", "content": "hello"}], "stream": False}
    session_id = "chat-session-logs"
    resp = client_with_logs.post("/v1/chat/completions", json=payload, headers={"X-Session-Id": session_id})
    assert resp.status_code == 200

    raw_dir = Path.cwd() / "logs" / "raw" / "openai_chat"
    assert sorted(raw_dir.glob("*-req-openai_chat.json"))
    assert sorted(raw_dir.glob("*-upstream-req-openai_chat.json"))
    assert sorted(raw_dir.glob("*-headers-openai_chat.json"))
    assert sorted(raw_dir.glob("*-upstream-res-openai_chat.json"))
    assert sorted(raw_dir.glob("*-downstream-res-openai_chat.json"))

    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob(f"*_{session_id}"))
    assert session_dirs
    session_dir = session_dirs[-1]
    req_files = sorted(session_dir.glob("*-req-openai_chat.json"))
    down_files = sorted(session_dir.glob("*-downstream-res-openai_chat.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-openai_chat.json"))
    assert req_files and down_files and non_stream_files

    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        obj = json.load(f)
    assert obj["usage"]["prompt_tokens"] == 2
    assert obj["usage"]["completion_tokens"] == 1

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.session_id_plugins.plugin_metadata import MetadataPlugin
from tests.support import FakeAsyncClient, FakeStreamResponse

_metadata_plugin = MetadataPlugin()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status_code", [200, 400])
@pytest.mark.parametrize("endpoint, model, upstream_format", [
    ("/v1/messages", "moonshot:kimi-k2.5", "chat"),
    ("/v1/messages", "moonshotAnthropic:claude-test", "anthropic"),
    ("/v1/chat/completions", "moonshot:kimi-k2.5", "chat"),
    ("/v1/responses", "codexOAuth:gpt-5.2-codex", "responses"),
])
def test_requests_do_not_write_logs_by_default(client, monkeypatch, stream, status_code, endpoint, model, upstream_format):
    monkeypatch.setenv("ENABLE_SESSION_INSPECTOR_UI", "true")
    if upstream_format == "anthropic":
        response_body = {
            "id": "msg_no_logs", "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": response_body},
            {"type": "message_stop"},
        ]
        sse = "".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events)
    elif upstream_format == "responses":
        response_body = {
            "id": "resp_no_logs", "object": "response",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        sse = f'data: {json.dumps({"type": "response.completed", "response": response_body})}\n\ndata: [DONE]\n\n'
    else:
        response_body = {
            "id": "chatcmpl_no_logs", "model": "kimi-k2.5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }
        sse = 'data: {"id":"chatcmpl_no_logs","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    if status_code >= 400:
        response_body = {"error": {"message": "rejected"}}
    FakeAsyncClient.post_response = httpx.Response(status_code, json=response_body)
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=status_code,
        lines=sse.splitlines(),
        raw_chunks=[sse.encode()],
        read_bytes=json.dumps(response_body).encode(),
    )
    payload = {"model": model, "stream": stream}
    if endpoint == "/v1/responses":
        payload["input"] = "hi"
    else:
        payload["messages"] = [{"role": "user", "content": "hi"}]

    response = client.post(endpoint, json=payload, headers={"X-Session-Id": "no-logs"})

    assert response.status_code == status_code
    assert ("ok" if status_code == 200 else "rejected") in response.text
    assert not (Path.cwd() / "logs").exists()


def test_extract_session_id_from_body_metadata_supports_legacy_and_json_string_formats():
    assert _metadata_plugin.extract(
        {}, {"metadata": {"user_id": "user_x_session_legacy-session"}}
    ) == "legacy-session"
    assert _metadata_plugin.extract(
        {}, {"metadata": {"user_id": '{"session_id":"json-session","uid":"id"}'}}
    ) == "json-session"


def test_messages_codex_oauth_stream_writes_session_logs(client_with_logs: TestClient):
    """测试 codex_oauth 流式请求会产出完整 session 日志文件。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.output_text.delta","delta":"ok"}',
            'data: {"type":"response.completed","response":{"id":"resp_stream_session","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":7,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "metadata": {"user_id": "user_x_session_codexstream"},
    }
    with client_with_logs.stream("POST", "/v1/messages", json=payload) as resp:
        _ = "".join(resp.iter_text())

    assert resp.status_code == 200
    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob("*_codexstream"))
    assert session_dirs, "应为 codex 流式请求生成 session 目录"
    session_dir = session_dirs[-1]

    req_files = sorted(session_dir.glob("*-req-anthropic_messages.json"))
    down_files = sorted(session_dir.glob("*-downstream-res-anthropic_messages.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-anthropic_messages.json"))

    assert req_files, "应保留 session 请求日志"
    assert down_files, "应生成 session 流式响应日志"
    assert non_stream_files, "应生成 session 非流式聚合日志"

    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        non_stream_obj = json.load(f)
    usage = non_stream_obj.get("usage") or {}
    assert non_stream_obj["object"] == "chat.completion"
    assert usage.get("prompt_tokens") == 7
    assert usage.get("completion_tokens") == 3


def test_messages_stream_writes_session_logs(client_with_logs: TestClient):
    """测试普通 messages 流式请求会产出 session 与 anthropic 分类日志。"""
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

    req_files = sorted(session_dir.glob("*-req-anthropic_messages.json"))
    down_files = sorted(session_dir.glob("*-downstream-res-anthropic_messages.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-anthropic_messages.json"))

    assert req_files, "应生成 session 请求日志"
    assert down_files, "应生成 session 流式响应日志"
    assert non_stream_files, "应生成 session 非流式聚合日志"

    with req_files[-1].open("r", encoding="utf-8") as f:
        req_obj = json.load(f)
    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        non_stream_obj = json.load(f)

    assert req_obj["model"] == "moonshot:kimi-k2.5"
    assert non_stream_obj["object"] == "chat.completion"
    assert "usage may omit provider-native fields" in non_stream_obj["_log_meta"]["usage_warning"]
    assert non_stream_obj["usage"]["prompt_tokens"] == 2
    assert non_stream_obj["usage"]["completion_tokens"] == 1

    raw_log_dir = Path.cwd() / "logs" / "raw" / "openai_chat"
    assert raw_log_dir.exists()
    assert sorted(raw_log_dir.glob("*-req-anthropic_messages.json")), "应生成 raw 请求日志"
    assert sorted(raw_log_dir.glob("*-upstream-req-anthropic_messages.json")), "应生成 raw 上游请求日志"
    assert sorted(raw_log_dir.glob("*-headers-anthropic_messages.json")), "应生成 raw 请求头日志"
    assert sorted(raw_log_dir.glob("*-upstream-res-anthropic_messages.json")), "应生成 raw 上游响应日志"
    assert sorted(raw_log_dir.glob("*-downstream-res-anthropic_messages.json")), "应生成 raw 下游响应日志"


def test_messages_stream_json_metadata_user_id_writes_session_logs_with_real_session_id(client_with_logs: TestClient):
    """测试 JSON 字符串 metadata.user_id 会使用真实 session_id 作为目录后缀。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"id":"chatcmpl_json_session","model":"kimi-k2.5","choices":[{"delta":{"content":"hello"},"finish_reason":null}],"usage":{"prompt_tokens":2,"completion_tokens":1}}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "moonshot:kimi-k2.5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "metadata": {"user_id": '{"session_id":"json-streamcase","uid":"id"}'},
    }
    with client_with_logs.stream("POST", "/v1/messages", json=payload) as resp:
        _ = "".join(resp.iter_text())

    assert resp.status_code == 200
    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob("*_json-streamcase"))
    assert session_dirs, "应为 JSON session_id 生成目录"
    assert not sorted(session_root.glob("*_id")), "不应把 JSON 内的 uid/id 误当作目录后缀"

import json
import copy
from pathlib import Path
from fastapi.testclient import TestClient
import zstandard as zstd

import app as app_module
from tests.support import FakeAsyncClient, FakeStreamResponse
from tests.support import TEST_UPSTREAM_CONFIG


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
    FakeAsyncClient.last_post_args = {}
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "store": True}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert resp.json() == upstream_body
    assert FakeAsyncClient.last_stream_args["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeAsyncClient.last_stream_args["headers"]["authorization"] == "Bearer codex-access-token"
    assert FakeAsyncClient.last_stream_args["headers"]["chatgpt-account-id"] == "org-test-account"
    assert FakeAsyncClient.last_stream_args["headers"]["originator"] == "codex_cli_rs"
    assert FakeAsyncClient.last_stream_args["headers"]["accept"] == "text/event-stream"
    assert "session_id" not in FakeAsyncClient.last_stream_args["headers"]
    assert "x-codex-turn-metadata" not in FakeAsyncClient.last_stream_args["headers"]
    assert FakeAsyncClient.last_stream_args["json"]["model"] == "gpt-5.2-codex"
    assert FakeAsyncClient.last_stream_args["json"]["store"] is False
    assert FakeAsyncClient.last_stream_args["json"]["stream"] is True
    assert FakeAsyncClient.last_stream_args["json"]["reasoning"]["effort"] == "medium"
    assert FakeAsyncClient.last_stream_args["json"]["include"] == ["reasoning.encrypted_content"]
    assert FakeAsyncClient.last_post_args == {}


def test_responses_codex_oauth_can_compress_non_stream_request_body(client: TestClient, monkeypatch):
    """测试 codex_oauth 可按 profile 配置压缩非流式请求体。"""
    cfg = copy.deepcopy(TEST_UPSTREAM_CONFIG)
    cfg["profiles"]["codexOAuth"]["features"] = {"enableRequestCompression": True}
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", cfg)

    upstream_body = {
        "id": "resp_compressed_non_stream",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "store": True}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200

    request_headers = FakeAsyncClient.last_stream_args["headers"]
    compressed = FakeAsyncClient.last_stream_args["content"]
    assert request_headers["content-encoding"] == "zstd"
    assert FakeAsyncClient.last_stream_args["json"] is None
    assert isinstance(compressed, bytes) and compressed

    decoded = json.loads(zstd.ZstdDecompressor().decompress(compressed).decode("utf-8"))
    assert decoded["model"] == "gpt-5.2-codex"
    assert decoded["store"] is False
    assert decoded["stream"] is True
    assert decoded["reasoning"]["effort"] == "medium"
    assert decoded["include"] == ["reasoning.encrypted_content"]


def test_responses_codex_oauth_can_compress_stream_request_body(client: TestClient, monkeypatch):
    """测试 codex_oauth 可按 profile 配置压缩流式请求体。"""
    cfg = copy.deepcopy(TEST_UPSTREAM_CONFIG)
    cfg["profiles"]["codexOAuth"]["features"] = {"enableRequestCompression": True}
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", cfg)

    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        raw_chunks=[b"data: [DONE]\n\n"],
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": True}
    with client.stream("POST", "/v1/responses", json=payload) as resp:
        _ = "".join(resp.iter_text())
    assert resp.status_code == 200

    request_headers = FakeAsyncClient.last_stream_args["headers"]
    compressed = FakeAsyncClient.last_stream_args["content"]
    assert request_headers["content-encoding"] == "zstd"
    assert FakeAsyncClient.last_stream_args["json"] is None
    assert isinstance(compressed, bytes) and compressed

    decoded = json.loads(zstd.ZstdDecompressor().decompress(compressed).decode("utf-8"))
    assert decoded["model"] == "gpt-5.2-codex"
    assert decoded["stream"] is True
    assert decoded["input"] == "hello"
    assert decoded["reasoning"]["effort"] == "medium"
    assert decoded["include"] == ["reasoning.encrypted_content"]


def test_responses_codex_oauth_all_accounts_cooling_down_returns_429(tmp_path, monkeypatch):
    """测试 responses 在 codex 账号全冷却时返回 429。"""
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
                        "cooldown_until": 4102444800,
                        "last_error": "",
                        "updated_at": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    local_client = TestClient(app_module.app)

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello"}
    resp = local_client.post("/v1/responses", json=payload)
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"


def test_responses_codex_oauth_model_suffix_sets_reasoning_effort(client: TestClient):
    """测试 /v1/responses 在 codex_oauth 下支持模型名 @high 后缀。"""
    upstream_body = {
        "id": "resp_passthrough_suffix",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex@high", "input": "hello"}
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["json"]["model"] == "gpt-5.2-codex"
    assert FakeAsyncClient.last_stream_args["json"]["reasoning"]["effort"] == "high"
    assert "reasoning.encrypted_content" in FakeAsyncClient.last_stream_args["json"]["include"]


def test_responses_explicit_reasoning_effort_overrides_model_suffix(client: TestClient):
    """测试 /v1/responses 显式 reasoning.effort 优先于模型名后缀。"""
    upstream_body = {
        "id": "resp_passthrough_suffix_override",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex@high",
        "input": "hello",
        "reasoning": {"effort": "low"},
    }
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["json"]["model"] == "gpt-5.2-codex"
    assert FakeAsyncClient.last_stream_args["json"]["reasoning"]["effort"] == "low"
    assert "reasoning.encrypted_content" in FakeAsyncClient.last_stream_args["json"]["include"]


def test_responses_codex_oauth_merges_custom_include_with_encrypted_reasoning(client: TestClient):
    """测试 /v1/responses + codex_oauth 会将 reasoning.encrypted_content 并入用户自定义 include。"""
    upstream_body = {
        "id": "resp_include_merge",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "input": "hello",
        "include": ["custom.item"],
    }
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    inc = FakeAsyncClient.last_stream_args["json"]["include"]
    assert "custom.item" in inc
    assert "reasoning.encrypted_content" in inc


def test_responses_codex_oauth_forwards_turn_metadata_and_session_id(client: TestClient):
    """测试 codex_oauth 上游请求会透传 codex turn metadata 与 session_id。"""
    upstream_body = {
        "id": "resp_passthrough_meta",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": False}
    turn_metadata = '{"turn_id":"turn_123"}'
    session_id = "session-header-id"
    resp = client.post(
        "/v1/responses",
        json=payload,
        headers={
            "x-codex-turn-metadata": turn_metadata,
            "session_id": session_id,
            "originator": "codex_cli_rs",
        },
    )
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["headers"]["x-codex-turn-metadata"] == turn_metadata
    assert FakeAsyncClient.last_stream_args["headers"]["session_id"] == session_id


def test_responses_codex_oauth_extracts_legacy_session_id_from_metadata_user_id(client: TestClient):
    """测试 session_id 请求头缺失时，会从旧格式 metadata.user_id 提取并上游透传。"""
    upstream_body = {
        "id": "resp_passthrough_meta_user",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "input": "hello",
        "metadata": {"user_id": "user_session_session-from-metadata"},
    }
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["headers"]["session_id"] == "session-from-metadata"


def test_responses_codex_oauth_extracts_json_session_id_from_metadata_user_id(client: TestClient):
    """测试 session_id 请求头缺失时，会从 JSON 字符串 metadata.user_id 提取真实 session_id。"""
    upstream_body = {
        "id": "resp_passthrough_meta_user_json",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "input": "hello",
        "metadata": {"user_id": json.dumps({"session_id": "session-from-json", "uid": "id"}, ensure_ascii=False)},
    }
    resp = client.post("/v1/responses", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["headers"]["session_id"] == "session-from-json"


def test_responses_codex_oauth_does_not_forward_turn_metadata_for_non_codex_downstream(client: TestClient):
    """测试非 codex 下游来源时，不透传 x-codex-turn-metadata。"""
    upstream_body = {
        "id": "resp_passthrough_meta_non_codex",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello"}
    resp = client.post(
        "/v1/responses",
        json=payload,
        headers={"x-codex-turn-metadata": '{"turn_id":"turn_non_codex"}'},
    )
    assert resp.status_code == 200
    assert "x-codex-turn-metadata" not in FakeAsyncClient.last_stream_args["headers"]


def test_responses_with_session_writes_raw_and_session_logs(client_with_logs: TestClient):
    """测试 responses 带 session 时同时写 raw(5) 与 session(3) 日志。"""
    upstream_body = {
        "id": "resp_session_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
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
    assert obj["object"] == "chat.completion"
    assert obj["usage"]["prompt_tokens"] == 3
    assert obj["usage"]["completion_tokens"] == 2


def test_responses_with_session_on_upstream_failure_skips_session_response_logs(client_with_logs: TestClient):
    """测试 responses 上游失败时，不写 session response/non-stream 日志。"""
    FakeAsyncClient.stream_response = FakeStreamResponse(
        500,
        read_bytes=b'{"error":"upstream_fail"}',
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": False}
    session_id = "responses-session-error-no-response-log"
    resp = client_with_logs.post("/v1/responses", json=payload, headers={"X-Session-Id": session_id})
    assert resp.status_code == 500

    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob(f"*_{session_id}"))
    assert session_dirs
    session_dir = session_dirs[-1]
    req_files = sorted(session_dir.glob("*-req-openai_responses.json"))
    down_files = sorted(session_dir.glob("*-downstream-res-openai_responses.json"))
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-openai_responses.json"))
    assert not req_files
    assert not down_files
    assert not non_stream_files


def test_responses_with_underscore_session_header_writes_session_logs(client_with_logs: TestClient):
    """测试 responses 支持 session_id 下划线请求头写入 session 日志。"""
    upstream_body = {
        "id": "resp_session_header_underscore",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        lines=[
            f'data: {json.dumps({"type": "response.completed", "response": upstream_body}, ensure_ascii=False)}',
            "data: [DONE]",
        ],
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": False}
    session_id = "responses-session-underscore-header"
    resp = client_with_logs.post("/v1/responses", json=payload, headers={"session_id": session_id})
    assert resp.status_code == 200

    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob(f"*_{session_id}"))
    assert session_dirs


def test_responses_stream_split_chunks_keeps_non_stream_text(client_with_logs: TestClient):
    """测试 responses 流式 completed 事件被拆块时，non-stream 仍可聚合文本。"""
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_split_chunks",
            "object": "response",
            "model": "gpt-5.3-codex",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "我在！有什么需要我做的？"}],
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 6},
        },
    }
    completed_line = f"event: response.completed\ndata: {json.dumps(completed, ensure_ascii=False)}\n\n"
    split_at = max(int(len(completed_line) * 0.55), 40)
    FakeAsyncClient.stream_response = FakeStreamResponse(
        200,
        raw_chunks=[
            completed_line[:split_at].encode("utf-8"),
            completed_line[split_at:].encode("utf-8"),
            b"data: [DONE]\n\n",
        ],
    )

    payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hello", "stream": True}
    session_id = "responses-stream-split"
    with client_with_logs.stream("POST", "/v1/responses", json=payload, headers={"X-Session-Id": session_id}) as resp:
        _ = "".join(resp.iter_text())
    assert resp.status_code == 200

    session_root = Path.cwd() / "logs" / "session"
    session_dirs = sorted(session_root.glob(f"*_{session_id}"))
    assert session_dirs
    session_dir = session_dirs[-1]
    non_stream_files = sorted(session_dir.glob("*-non-stream-res-openai_responses.json"))
    assert non_stream_files
    with non_stream_files[-1].open("r", encoding="utf-8") as f:
        obj = json.load(f)
    assert obj["choices"][0]["message"]["content"] == "我在！有什么需要我做的？"

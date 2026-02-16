import httpx
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

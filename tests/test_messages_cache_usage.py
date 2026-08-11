import json

import httpx
from fastapi.testclient import TestClient

from tests.support import FakeAsyncClient, FakeStreamResponse


def _codex_completed_line() -> str:
    return (
        'data: '
        + json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_cache",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "cached"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {
                            "cached_tokens": 80,
                            "cache_write_tokens": 5,
                        },
                        "output_tokens": 7,
                    },
                },
            }
        )
    )


def test_messages_codex_non_stream_passes_cache_usage(client: TestClient):
    FakeAsyncClient.stream_response = FakeStreamResponse(
        lines=[_codex_completed_line(), "data: [DONE]"]
    )

    response = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
        },
    )

    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["input_tokens"] == 15
    assert usage["output_tokens"] == 7
    assert usage["cache_read_input_tokens"] == 80
    assert usage["cache_creation_input_tokens"] == 5


def test_messages_codex_stream_passes_cache_usage(client: TestClient):
    FakeAsyncClient.stream_response = FakeStreamResponse(
        lines=[_codex_completed_line(), "data: [DONE]"]
    )

    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "stream": True,
        },
    ) as response:
        events = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"cache_read_input_tokens": 80' in events
    assert '"cache_creation_input_tokens": 5' in events


def test_messages_openai_non_stream_maps_cache_usage(client: TestClient):
    FakeAsyncClient.post_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={
            "choices": [{"message": {"content": "cached"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 5},
                "completion_tokens": 7,
            },
        },
    )

    response = client.post(
        "/v1/messages",
        json={
            "model": "moonshot:kimi-k2.5",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
        },
    )

    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["cache_read_input_tokens"] == 80
    assert usage["cache_creation_input_tokens"] == 5

from fastapi.testclient import TestClient

from tests.support import FakeAsyncClient, FakeStreamResponse


def _set_codex_success_response() -> None:
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_effort","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}}',
            "data: [DONE]",
        ],
    )


def test_messages_maps_anthropic_output_config_effort(client: TestClient):
    _set_codex_success_response()

    response = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.6-luna",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "output_config": {"effort": "high"},
        },
    )

    assert response.status_code == 200
    assert FakeAsyncClient.last_stream_args["json"]["reasoning"]["effort"] == "high"


def test_messages_maps_anthropic_max_effort_to_codex_max(client: TestClient):
    _set_codex_success_response()

    response = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.6-luna",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "output_config": {"effort": "max"},
        },
    )

    assert response.status_code == 200
    assert FakeAsyncClient.last_stream_args["json"]["reasoning"]["effort"] == "max"


def test_messages_output_config_effort_overrides_model_suffix(client: TestClient):
    _set_codex_success_response()

    response = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.6-luna@low",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 128,
            "output_config": {"effort": "high"},
        },
    )

    assert response.status_code == 200
    upstream = FakeAsyncClient.last_stream_args["json"]
    assert upstream["model"] == "gpt-5.6-luna"
    assert upstream["reasoning"]["effort"] == "high"

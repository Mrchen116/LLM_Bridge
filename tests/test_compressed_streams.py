import gzip
import json
import zlib

import httpx
import pytest
import zstandard

from tests.support import FakeAsyncClient


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        # Exercise HTTPX's streaming decoder across compressed frame boundaries.
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate", "zstd"])
@pytest.mark.parametrize("endpoint", ["messages", "responses"])
def test_passthrough_decodes_compressed_sse(client, monkeypatch, endpoint, encoding):
    if endpoint == "messages":
        events = [
            {"type": "message_start", "message": {"id": "msg_compressed", "type": "message", "role": "assistant", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "compressed stream works"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        payload = {"model": "moonshotAnthropic:claude-test", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "stream": True}
    else:
        events = [
            {"type": "response.output_text.delta", "delta": "compressed stream works"},
            {"type": "response.completed", "response": {"id": "resp_compressed", "status": "completed", "output": []}},
        ]
        payload = {"model": "codexOAuth:gpt-5.2-codex", "input": "hi", "stream": True}

    sse = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
    compress = {"identity": lambda value: value, "gzip": gzip.compress, "deflate": zlib.compress, "zstd": zstandard.ZstdCompressor().compress}[encoding]
    headers = {"content-type": "text/event-stream"}
    if encoding != "identity":
        headers["content-encoding"] = encoding
    upstream = httpx.Response(200, headers=headers, stream=ChunkedBody(compress(sse)))
    monkeypatch.setattr(FakeAsyncClient, "stream_response", upstream)
    monkeypatch.setattr(FakeAsyncClient, "stream_responses", [])

    response = client.post(f"/v1/{endpoint}", json=payload)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "content-encoding" not in response.headers
    assert response.content == sse

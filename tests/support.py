import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


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
    stream_responses: List[FakeStreamResponse] = []
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
        if FakeAsyncClient.stream_responses:
            return FakeStreamContext(FakeAsyncClient.stream_responses.pop(0))
        return FakeStreamContext(FakeAsyncClient.stream_response)


class ConnectErrorStreamContext:
    async def __aenter__(self):
        raise httpx.ConnectError("mock connect failure")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class ConnectErrorAsyncClient(FakeAsyncClient):
    def stream(self, method: str, url: str, headers: Dict[str, str], json: Dict[str, Any]):
        FakeAsyncClient.last_stream_args = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
        }
        return ConnectErrorStreamContext()

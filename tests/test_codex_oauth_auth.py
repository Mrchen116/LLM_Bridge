import json
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import token_auth
from tests.support import FakeAsyncClient, FakeStreamResponse


def test_codex_oauth_upstream_use_store_token(client: TestClient, monkeypatch):
    """测试未设置环境变量时会使用本地 oauth 存储中的 access token。"""
    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_ACCOUNT_ID", raising=False)
    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "codex_oauth": {
                    "access_token": "oauth-access-from-store",
                    "refresh_token": "oauth-refresh",
                    "expires_at": 4102444800,
                    "account_id": "",
                    "updated_at": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_2","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":3,"output_tokens":2}}}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    upstream_resp = client.post("/v1/chat/completions", json=payload)
    assert upstream_resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["headers"]["Authorization"] == "Bearer oauth-access-from-store"


def test_codex_oauth_auto_refresh_on_upstream_call(client: TestClient, monkeypatch):
    """测试 access token 过期时会自动刷新并写回新 refresh token。"""
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
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_3","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":3,"output_tokens":2}}}',
            "data: [DONE]",
        ],
    )
    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert FakeAsyncClient.last_stream_args["headers"]["Authorization"] == "Bearer refreshed-access"

    store = json.loads(Path(".codex_oauth.json").read_text(encoding="utf-8"))
    assert store["codex_oauth"]["refresh_token"] == "refresh-new"

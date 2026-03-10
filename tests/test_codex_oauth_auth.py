import json
import sys
import copy
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import token_auth
import app as app_module
import src.handlers.chat_completions as chat_handler
from tests.support import FakeAsyncClient, FakeStreamResponse


def test_codex_oauth_upstream_use_store_token(client: TestClient, monkeypatch):
    """测试未设置环境变量时会使用本地 oauth 存储中的 access token。"""
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
    assert FakeAsyncClient.last_stream_args["headers"]["authorization"] == "Bearer oauth-access-from-store"


def test_codex_oauth_auto_refresh_on_upstream_call(client: TestClient, monkeypatch):
    """测试 access token 过期时会自动刷新并写回新 refresh token。"""
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
    assert FakeAsyncClient.last_stream_args["headers"]["authorization"] == "Bearer refreshed-access"

    store = json.loads(Path(".codex_oauth.json").read_text(encoding="utf-8"))
    assert store["schema_version"] == 2
    assert store["accounts"][0]["refresh_token"] == "refresh-new"


def test_codex_oauth_failover_on_429_switches_account(client: TestClient, monkeypatch):
    """测试 429 会触发账号切换到下一个可用 label。"""
    cfg = copy.deepcopy(app_module.UPSTREAM_CONFIG)
    codex = cfg["profiles"]["codexOAuth"]
    codex["defaults"]["retryMax"] = 3
    codex["auth"] = {
        "codexEndpoint": "https://chatgpt.com/backend-api/codex/responses",
        "accountPoolPolicy": {"maxFailoverPerRequest": 2, "cooldownSeconds": 300},
    }
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", cfg)

    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_label": "primary",
                "accounts": [
                    {
                        "label": "primary",
                        "account_id": "org-primary",
                        "priority": 100,
                        "enabled": True,
                        "access_token": "token-a",
                        "refresh_token": "refresh-a",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                    {
                        "label": "backup",
                        "account_id": "org-backup",
                        "priority": 200,
                        "enabled": True,
                        "access_token": "token-b",
                        "refresh_token": "refresh-b",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_collect_codex_response_from_stream(client, upstream_url, headers, request_body):
        auth = str(headers.get("authorization") or headers.get("Authorization") or "")
        if auth == "Bearer token-a":
            return {
                "ok": False,
                "status_code": 429,
                "error_bytes": b'{\"error\":\"rate_limited\"}',
                "error_text": "{\"error\":\"rate_limited\"}",
                "chunks": [{"type": "error_body", "body": "{\"error\":\"rate_limited\"}"}],
            }
        return {
            "ok": True,
            "status_code": 200,
            "response_json": {
                "id": "resp_ok",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "chunks": ["data: [DONE]"],
        }

    monkeypatch.setattr(chat_handler, "collect_codex_response_from_stream", fake_collect_codex_response_from_stream)

    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"

    store = json.loads(Path(".codex_oauth.json").read_text(encoding="utf-8"))
    accounts = {item["label"]: item for item in store["accounts"]}
    assert int(accounts["primary"]["cooldown_until"]) > 0


def test_codex_oauth_failover_on_insufficient_quota_switches_account(client: TestClient, monkeypatch):
    """测试非 429 场景下，insufficient_quota 错误码也会触发切换。"""
    cfg = copy.deepcopy(app_module.UPSTREAM_CONFIG)
    codex = cfg["profiles"]["codexOAuth"]
    codex["defaults"]["retryMax"] = 3
    codex["auth"] = {
        "codexEndpoint": "https://chatgpt.com/backend-api/codex/responses",
        "accountPoolPolicy": {"maxFailoverPerRequest": 2, "cooldownSeconds": 300},
    }
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", cfg)

    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_label": "primary",
                "accounts": [
                    {
                        "label": "primary",
                        "account_id": "org-primary",
                        "priority": 100,
                        "enabled": True,
                        "access_token": "token-a",
                        "refresh_token": "refresh-a",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                    {
                        "label": "backup",
                        "account_id": "org-backup",
                        "priority": 200,
                        "enabled": True,
                        "access_token": "token-b",
                        "refresh_token": "refresh-b",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    async def fake_collect_codex_response_from_stream(client, upstream_url, headers, request_body):
        auth = str(headers.get("authorization") or headers.get("Authorization") or "")
        if auth == "Bearer token-a":
            err_text = "{\"error\":{\"code\":\"insufficient_quota\",\"message\":\"quota exceeded\"}}"
            return {
                "ok": False,
                "status_code": 403,
                "error_bytes": err_text.encode("utf-8"),
                "error_text": err_text,
                "chunks": [{"type": "error_body", "body": err_text}],
            }
        return {
            "ok": True,
            "status_code": 200,
            "response_json": {
                "id": "resp_ok",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "chunks": ["data: [DONE]"],
        }

    monkeypatch.setattr(chat_handler, "collect_codex_response_from_stream", fake_collect_codex_response_from_stream)

    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"

    store = json.loads(Path(".codex_oauth.json").read_text(encoding="utf-8"))
    accounts = {item["label"]: item for item in store["accounts"]}
    assert int(accounts["primary"]["cooldown_until"]) > 0


def test_codex_oauth_retry_max_is_not_capped_by_failover_limit(client: TestClient, monkeypatch):
    """测试 server_error 同账号重试次数不受 maxFailoverPerRequest 限制。"""
    cfg = copy.deepcopy(app_module.UPSTREAM_CONFIG)
    codex = cfg["profiles"]["codexOAuth"]
    codex["defaults"]["retryMax"] = 4
    codex["auth"] = {
        "codexEndpoint": "https://chatgpt.com/backend-api/codex/responses",
        "accountPoolPolicy": {"maxFailoverPerRequest": 1, "cooldownSeconds": 300},
    }
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", cfg)

    Path(".codex_oauth.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "default_label": "primary",
                "accounts": [
                    {
                        "label": "primary",
                        "account_id": "org-primary",
                        "priority": 100,
                        "enabled": True,
                        "access_token": "token-a",
                        "refresh_token": "refresh-a",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                    {
                        "label": "backup",
                        "account_id": "org-backup",
                        "priority": 200,
                        "enabled": True,
                        "access_token": "token-b",
                        "refresh_token": "refresh-b",
                        "expires_at": 4102444800,
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    call_headers = []

    async def fake_collect_codex_response_from_stream(client, upstream_url, headers, request_body):
        auth = str(headers.get("authorization") or headers.get("Authorization") or "")
        call_headers.append(auth)
        if len(call_headers) < 4:
            err_text = "{\"error\":{\"code\":\"server_error\",\"message\":\"temporary upstream failure\"}}"
            return {
                "ok": False,
                "status_code": 502,
                "error_bytes": err_text.encode("utf-8"),
                "error_text": err_text,
                "chunks": [{"type": "error_body", "body": err_text}],
            }
        return {
            "ok": True,
            "status_code": 200,
            "response_json": {
                "id": "resp_ok",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "chunks": ["data: [DONE]"],
        }

    monkeypatch.setattr(chat_handler, "collect_codex_response_from_stream", fake_collect_codex_response_from_stream)

    payload = {"model": "codexOAuth:gpt-5.2-codex", "messages": [{"role": "user", "content": "hello"}]}
    resp = client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok"
    assert call_headers == ["Bearer token-a", "Bearer token-a", "Bearer token-a", "Bearer token-a"]

import sys
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as app_module
import proxy_logging
from src.reasoning import reinject
from src.state.sqlite_store import PersistentSqliteReasoningStore
from tests.support import FakeAsyncClient, TEST_UPSTREAM_CONFIG


@pytest.fixture(autouse=True)
def isolate_reasoning_cache(tmp_path, monkeypatch):
    store = PersistentSqliteReasoningStore(tmp_path / "reasoning.sqlite3")
    monkeypatch.setattr(reinject, "_REASONING_STORE", store)
    yield
    store.close()


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(proxy_logging, "REQUEST_LOGGING_ENABLED", False)
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
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module, "_dump_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)


@pytest.fixture()
def client_with_logs(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(proxy_logging, "REQUEST_LOGGING_ENABLED", True)
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
                        "cooldown_until": 0,
                        "last_error": "",
                        "updated_at": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as app_module
from tests.support import FakeAsyncClient, TEST_UPSTREAM_CONFIG


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "codex-access-token")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "org-test-account")
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module, "_dump_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)


@pytest.fixture()
def client_with_logs(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "codex-access-token")
    monkeypatch.setenv("CODEX_ACCOUNT_ID", "org-test-account")
    monkeypatch.setattr(app_module, "UPSTREAM_CONFIG", TEST_UPSTREAM_CONFIG)
    monkeypatch.setattr(app_module, "BAN_STREAM", False)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", FakeAsyncClient)
    return TestClient(app_module.app)

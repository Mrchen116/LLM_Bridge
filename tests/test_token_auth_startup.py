from pathlib import Path
import sys
import asyncio

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import token_auth


def test_ensure_codex_login_for_startup_skip_when_authorized(monkeypatch):
    """测试启动检查在已授权且可用时直接通过。"""

    async def fake_once():
        return False

    monkeypatch.setattr(token_auth, "_ensure_codex_login_once", fake_once)
    monkeypatch.setenv("CODEX_STARTUP_RETRY_SECONDS", "0")

    done = asyncio.run(token_auth.ensure_codex_login_for_startup())
    assert done is False


def test_ensure_codex_login_once_raises_when_no_enabled_account(monkeypatch):
    """无启用账号时应永久失败，避免无意义重试。"""

    async def fake_status():
        return {"authorized": False, "enabled_accounts": 0}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)

    with pytest.raises(token_auth.CodexStartupAuthError) as exc:
        asyncio.run(token_auth._ensure_codex_login_once())

    assert exc.value.permanent is True
    assert "python manage_codex_accounts.py" in str(exc.value)


def test_ensure_codex_login_once_tries_login_all_even_when_not_authorized(monkeypatch):
    """冷却等导致 authorized=False 时仍应尝试 login-all refresh。"""

    force_refresh_values = []

    async def fake_status():
        return {"authorized": False, "enabled_accounts": 1}

    async def fake_login_all(*, force_refresh=True):
        force_refresh_values.append(force_refresh)
        return {"ok": 1, "failed": 0, "details": [{"label": "myself", "ok": True}]}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)
    monkeypatch.setattr(token_auth, "login_all_codex_accounts", fake_login_all)

    done = asyncio.run(token_auth._ensure_codex_login_once())
    assert done is False
    assert force_refresh_values == [False]


def test_ensure_codex_login_for_startup_retries_transient_failure(monkeypatch):
    """瞬时 refresh 失败应在窗口内重试，成功后通过。"""
    calls = {"n": 0}

    async def flaky_once():
        calls["n"] += 1
        if calls["n"] < 3:
            raise token_auth.CodexStartupAuthError(
                "temporary refresh failure",
                permanent=False,
            )
        return False

    sleeps: list[float] = []

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(token_auth, "_ensure_codex_login_once", flaky_once)
    monkeypatch.setattr(token_auth.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("CODEX_STARTUP_RETRY_SECONDS", "60")
    monkeypatch.setenv("CODEX_STARTUP_RETRY_INTERVAL_SECONDS", "1")

    done = asyncio.run(token_auth.ensure_codex_login_for_startup())
    assert done is False
    assert calls["n"] == 3
    assert sleeps == [1.0, 1.0]


def test_ensure_codex_login_for_startup_does_not_retry_permanent_error(monkeypatch):
    """永久配置错误应立即失败，不进入重试窗口。"""
    calls = {"n": 0}

    async def permanent_once():
        calls["n"] += 1
        raise token_auth.CodexStartupAuthError("no accounts", permanent=True)

    async def fake_sleep(_seconds: float):
        raise AssertionError("permanent error should not sleep/retry")

    monkeypatch.setattr(token_auth, "_ensure_codex_login_once", permanent_once)
    monkeypatch.setattr(token_auth.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("CODEX_STARTUP_RETRY_SECONDS", "60")

    with pytest.raises(token_auth.CodexStartupAuthError):
        asyncio.run(token_auth.ensure_codex_login_for_startup())

    assert calls["n"] == 1


def test_login_all_failure_is_permanent_for_missing_refresh_token():
    summary = {
        "ok": 0,
        "failed": 1,
        "details": [
            {"label": "myself", "ok": False, "error": "账号 myself 缺少 refresh_token，请重新登录"},
        ],
    }
    assert token_auth._login_all_failure_is_permanent(summary) is True


def test_login_all_failure_is_permanent_for_reused_refresh_token():
    summary = {
        "ok": 0,
        "failed": 1,
        "details": [
            {
                "label": "myself",
                "ok": False,
                "error": "token refresh failed: 401 refresh_token_reused",
            },
        ],
    }
    assert token_auth._login_all_failure_is_permanent(summary) is True


def test_login_all_failure_is_not_permanent_for_network_error():
    summary = {
        "ok": 0,
        "failed": 1,
        "details": [
            {"label": "myself", "ok": False, "error": "ConnectTimeout"},
        ],
    }
    assert token_auth._login_all_failure_is_permanent(summary) is False

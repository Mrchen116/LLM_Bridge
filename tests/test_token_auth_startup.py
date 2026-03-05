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

    async def fake_status():
        return {"authorized": True}

    async def fake_login_all():
        return {"ok": 1, "failed": 0, "details": []}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)
    monkeypatch.setattr(token_auth, "login_all_codex_accounts", fake_login_all)

    done = asyncio.run(token_auth.ensure_codex_login_for_startup())
    assert done is False


def test_ensure_codex_login_for_startup_raises_when_no_available_account(monkeypatch):
    """测试启动检查在无可用账号时给出明确提示。"""

    async def fake_status():
        return {"authorized": False}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(token_auth.ensure_codex_login_for_startup())

    assert "python manage_codex_accounts.py" in str(exc.value)

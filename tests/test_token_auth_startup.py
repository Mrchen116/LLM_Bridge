from pathlib import Path
import sys
import asyncio

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import token_auth


def test_ensure_codex_login_for_startup_skip_when_authorized(monkeypatch):
    """测试启动检查在已授权状态下跳过设备登录流程。"""
    async def fake_status():
        return {"authorized": True}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)
    called = {"start": 0}

    async def fake_start():
        called["start"] += 1
        return {}

    monkeypatch.setattr(token_auth, "start_codex_device_oauth", fake_start)
    done = asyncio.run(token_auth.ensure_codex_login_for_startup())
    assert done is False
    assert called["start"] == 0


def test_ensure_codex_login_for_startup_device_flow(monkeypatch):
    """测试启动检查在未授权状态下会完整执行设备登录流程。"""
    async def fake_status():
        return {"authorized": False}

    async def fake_start():
        return {
            "device_auth_id": "dev-1",
            "user_code": "ABCD-1234",
            "verification_url": "https://auth.openai.com/codex/device",
            "interval_seconds": 1,
        }

    called = {"finish": 0}

    async def fake_finish(device_auth_id: str, user_code: str, interval_seconds: int):
        called["finish"] += 1
        assert device_auth_id == "dev-1"
        assert user_code == "ABCD-1234"
        assert interval_seconds == 1
        return {"expires_at": 123, "account_id": "org_xxx"}

    monkeypatch.setattr(token_auth, "get_codex_auth_status", fake_status)
    monkeypatch.setattr(token_auth, "start_codex_device_oauth", fake_start)
    monkeypatch.setattr(token_auth, "finish_codex_device_oauth", fake_finish)

    done = asyncio.run(token_auth.ensure_codex_login_for_startup())
    assert done is True
    assert called["finish"] == 1

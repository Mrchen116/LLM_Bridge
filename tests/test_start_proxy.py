from pathlib import Path
import sys
import argparse
import os

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from start_proxy import _apply_startup_env_flags, _has_codex_oauth_profile
from upstream_config import PROTOCOL_OPENAI_CHAT, resolve_profile


def test_has_codex_oauth_profile_true():
    """测试配置包含 codex_oauth profile 时识别结果为真。"""
    cfg = {
        "defaultProfile": "codexOAuth",
        "profiles": {
            "codexOAuth": {
                "provider": "codex_oauth",
                "baseUrl": "https://api.openai.com/v1",
                "auth": {},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "gpt-5.2-codex"},
            }
        },
    }
    assert _has_codex_oauth_profile(cfg) is True


def test_has_codex_oauth_profile_false():
    """测试配置不包含 codex_oauth profile 时识别结果为假。"""
    cfg = {
        "defaultProfile": "moonshot",
        "profiles": {
            "moonshot": {
                "provider": "openai_compatible",
                "baseUrl": "https://api.moonshot.cn/v1",
                "auth": {"apiKeyEnv": "MOONSHOT_API_KEY"},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "kimi-k2.5"},
            }
        },
    }
    assert _has_codex_oauth_profile(cfg) is False


def test_apply_startup_env_flags_sets_ui_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENABLE_SESSION_INSPECTOR_UI", raising=False)
    args = argparse.Namespace(ban_explore=False, ban_stream=False, ui=True, open_ui=False)
    _apply_startup_env_flags(args)
    assert "ENABLE_SESSION_INSPECTOR_UI" in os.environ
    assert os.environ["ENABLE_SESSION_INSPECTOR_UI"] == "true"


def test_apply_startup_env_flags_open_ui_implies_ui_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENABLE_SESSION_INSPECTOR_UI", raising=False)
    args = argparse.Namespace(ban_explore=False, ban_stream=False, ui=False, open_ui=True)
    _apply_startup_env_flags(args)
    assert os.environ.get("ENABLE_SESSION_INSPECTOR_UI") == "true"


def test_resolve_profile_supports_byenv_with_reasoning_suffix():
    cfg = {
        "defaultProfile": "codexOAuth",
        "profiles": {
            "codexOAuth": {
                "provider": "codex_oauth",
                "baseUrl": "https://api.openai.com/v1",
                "auth": {},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "gpt-5.4"},
            }
        },
    }

    resolved = resolve_profile(cfg, {"model": "byenv@high"}, PROTOCOL_OPENAI_CHAT)
    assert resolved.profile_name == "codexOAuth"
    assert resolved.model == "gpt-5.4"
    assert resolved.reasoning_effort == "high"


def test_resolve_profile_supports_max_reasoning_suffix():
    cfg = {
        "defaultProfile": "codexOAuth",
        "profiles": {
            "codexOAuth": {
                "provider": "codex_oauth",
                "baseUrl": "https://api.openai.com/v1",
                "auth": {},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "gpt-5.6-luna"},
            }
        },
    }

    resolved = resolve_profile(cfg, {"model": "byenv@max"}, PROTOCOL_OPENAI_CHAT)
    assert resolved.model == "gpt-5.6-luna"
    assert resolved.reasoning_effort == "max"


def test_resolve_profile_supports_profile_prefixed_byenv_with_reasoning_suffix():
    cfg = {
        "defaultProfile": "moonshot",
        "profiles": {
            "moonshot": {
                "provider": "openai_compatible",
                "baseUrl": "https://api.moonshot.cn/v1",
                "auth": {"apiKeyEnv": "MOONSHOT_API_KEY"},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "kimi-k2.5"},
            },
            "codexOAuth": {
                "provider": "codex_oauth",
                "baseUrl": "https://api.openai.com/v1",
                "auth": {},
                "capabilities": {"ingress": ["openai_chat"]},
                "defaults": {"model": "gpt-5.4"},
            },
        },
    }

    resolved = resolve_profile(cfg, {"model": "codexOAuth:byenv@high"}, PROTOCOL_OPENAI_CHAT)
    assert resolved.profile_name == "codexOAuth"
    assert resolved.model == "gpt-5.4"
    assert resolved.reasoning_effort == "high"

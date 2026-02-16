from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from start_proxy import _has_codex_oauth_profile


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

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    build_auth_headers,
    build_upstream_url,
    get_effective_auth_type,
    get_runtime_options,
    load_and_validate_config,
)


# 真实上游联通性测试，默认不跑，避免影响日常单测与 CI。
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_UPSTREAM_TESTS", "0") != "1",
    reason="需要显式设置 RUN_LIVE_UPSTREAM_TESTS=1 才执行真实上游测试",
)


ROOT_DIR = Path(__file__).resolve().parents[1]
UPSTREAMS_PATH = ROOT_DIR / "upstreams.json"

# 加载 .env，便于读取真实 API Key。
load_dotenv(override=True)


def _iter_profile_ingress_cases(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    cases: List[Tuple[str, str]] = []
    for profile_name, profile in cfg.get("profiles", {}).items():
        ingress = (profile.get("capabilities") or {}).get("ingress") or []
        for protocol in ingress:
            if protocol in {PROTOCOL_ANTHROPIC_MESSAGES, PROTOCOL_OPENAI_CHAT, PROTOCOL_OPENAI_RESPONSES}:
                cases.append((profile_name, protocol))
    return cases


def _minimal_payload(profile: Dict[str, Any], protocol: str, model: str) -> Dict[str, Any]:
    if protocol == PROTOCOL_ANTHROPIC_MESSAGES:
        if profile.get("provider") == "anthropic":
            return {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        # openai_compatible 的 anthropic_messages 入口在上游实际是 /chat/completions。
        return {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }

    if protocol == PROTOCOL_OPENAI_RESPONSES:
        return {
            "model": model,
            "input": "ping",
            "max_output_tokens": 1,
        }

    # PROTOCOL_OPENAI_CHAT
    return {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }


def _required_envs_for_profile(profile: Dict[str, Any]) -> List[str]:
    auth = profile.get("auth") or {}
    auth_type = get_effective_auth_type(profile)
    if auth_type in {"bearer", "anthropic_key"}:
        return [str(auth.get("apiKeyEnv") or "")]
    if auth_type == "codex_oauth":
        required = [str(auth.get("accessTokenEnv") or "CODEX_ACCESS_TOKEN")]
        account_env = str(auth.get("accountIdEnv") or "CODEX_ACCOUNT_ID")
        if account_env:
            required.append(account_env)
        return required
    if auth_type == "internal_hw":
        return [
            str(auth.get("apiKeyEnv") or ""),
            str(auth.get("hwIdEnv") or ""),
            str(auth.get("hwAppKeyEnv") or ""),
        ]
    return []


LIVE_CFG = load_and_validate_config(str(UPSTREAMS_PATH))
LIVE_CASES = _iter_profile_ingress_cases(LIVE_CFG)


@pytest.mark.parametrize("profile_name,protocol", LIVE_CASES)
def test_live_upstream_reachable(profile_name: str, protocol: str):
    """测试真实上游在给定 profile/protocol 下链路可达。"""
    profile = LIVE_CFG["profiles"][profile_name]
    model = str((profile.get("defaults") or {}).get("model") or "")

    required_envs = [name for name in _required_envs_for_profile(profile) if name]
    missing_envs = [name for name in required_envs if not os.getenv(name)]
    if missing_envs:
        pytest.skip(f"profile={profile_name} 缺少环境变量: {', '.join(missing_envs)}")

    # internal_hw 需要动态 token；当前仓库 token_auth 是占位实现，先跳过。
    if get_effective_auth_type(profile) == "internal_hw":
        pytest.skip("internal_hw 依赖动态 token_auth，实现就绪后再开启此用例")

    url = build_upstream_url(profile, protocol)
    headers = build_auth_headers(profile, model)
    verify, timeout_seconds, _, trust_env = get_runtime_options(profile)
    payload = _minimal_payload(profile, protocol, model)

    timeout = min(float(timeout_seconds), 30.0)
    with httpx.Client(verify=verify, timeout=timeout, trust_env=trust_env) as client:
        resp = client.post(url, headers=headers, json=payload)

    # 联通性判断：只要不是路由不存在/方法不允许/服务端错误，就认为链路可达。
    assert resp.status_code not in {404, 405}, (
        f"profile={profile_name}, protocol={protocol}, url={url}, status={resp.status_code}, body={resp.text[:400]}"
    )
    assert resp.status_code < 500, (
        f"profile={profile_name}, protocol={protocol}, url={url}, status={resp.status_code}, body={resp.text[:400]}"
    )

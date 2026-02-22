import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from token_auth import get_codex_auth_status
from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    get_effective_auth_type,
    load_and_validate_config,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_PROXY_E2E_TESTS", "0") != "1",
    reason="需要显式设置 RUN_LIVE_PROXY_E2E_TESTS=1 才执行代理 live E2E 测试",
)


load_dotenv(override=True)
UPSTREAMS_PATH = ROOT_DIR / "upstreams.json"
LIVE_CFG = load_and_validate_config(str(UPSTREAMS_PATH))


def _iter_profile_ingress_cases(cfg: Dict[str, Any]) -> List[Tuple[str, str]]:
    cases: List[Tuple[str, str]] = []
    for profile_name, profile in cfg.get("profiles", {}).items():
        ingress = (profile.get("capabilities") or {}).get("ingress") or []
        for protocol in ingress:
            if protocol in {PROTOCOL_ANTHROPIC_MESSAGES, PROTOCOL_OPENAI_CHAT, PROTOCOL_OPENAI_RESPONSES}:
                cases.append((profile_name, protocol))
    return cases


LIVE_CASES = _iter_profile_ingress_cases(LIVE_CFG)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _required_envs_for_profile(profile: Dict[str, Any]) -> List[str]:
    auth = profile.get("auth") or {}
    auth_type = get_effective_auth_type(profile)
    if auth_type in {"bearer", "anthropic_key"}:
        return [str(auth.get("apiKeyEnv") or "")]
    if auth_type == "internal_hw":
        return [
            str(auth.get("apiKeyEnv") or ""),
            str(auth.get("hwIdEnv") or ""),
            str(auth.get("hwAppKeyEnv") or ""),
        ]
    return []


def _proxy_path_for_protocol(protocol: str) -> str:
    if protocol == PROTOCOL_ANTHROPIC_MESSAGES:
        return "/v1/messages"
    if protocol == PROTOCOL_OPENAI_CHAT:
        return "/v1/chat/completions"
    if protocol == PROTOCOL_OPENAI_RESPONSES:
        return "/v1/responses"
    raise AssertionError(f"unsupported protocol: {protocol}")


def _proxy_payload(profile_name: str, profile: Dict[str, Any], protocol: str) -> Dict[str, Any]:
    model = str((profile.get("defaults") or {}).get("model") or "")
    route_model = f"{profile_name}:{model}"
    if protocol == PROTOCOL_ANTHROPIC_MESSAGES:
        return {
            "model": route_model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    if protocol == PROTOCOL_OPENAI_CHAT:
        return {
            "model": route_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 16,
            "stream": False,
        }
    return {
        "model": route_model,
        "instructions": "You are a concise assistant.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "stream": False,
    }


@pytest.fixture(scope="module")
def live_proxy_base_url():
    port = _pick_free_port()
    env = os.environ.copy()
    env["PROXY_HOST"] = "127.0.0.1"
    env["PROXY_PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30.0
    last_err = ""
    with httpx.Client(timeout=2.0, trust_env=False) as client:
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"proxy failed to start, exit={proc.returncode}, output={output}")
            try:
                r = client.get(f"{base_url}/health")
                if r.status_code == 200:
                    yield base_url
                    break
            except Exception as e:
                last_err = str(e)
            time.sleep(0.2)
        else:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"proxy health check timeout, last_err={last_err}, output={output}")

    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.parametrize("profile_name,protocol", LIVE_CASES)
def test_proxy_live_e2e_profile_ingress(profile_name: str, protocol: str, live_proxy_base_url: str):
    profile = LIVE_CFG["profiles"][profile_name]
    auth_type = get_effective_auth_type(profile)

    missing_envs = [name for name in _required_envs_for_profile(profile) if name and not os.getenv(name)]
    if missing_envs:
        pytest.skip(f"profile={profile_name} 缺少环境变量: {', '.join(missing_envs)}")
    if auth_type == "internal_hw":
        pytest.skip("internal_hw 依赖动态 token_auth，实现就绪后再开启此用例")
    if auth_type == "codex_oauth":
        status = asyncio.run(get_codex_auth_status())
        if not bool(status.get("authorized")):
            pytest.skip("codex_oauth 未授权（环境变量与本地 oauth store 都不可用）")

    payload = _proxy_payload(profile_name, profile, protocol)
    path = _proxy_path_for_protocol(protocol)

    with httpx.Client(timeout=60.0, trust_env=False) as client:
        resp = client.post(f"{live_proxy_base_url}{path}", json=payload)

    assert resp.status_code not in {404, 405}, (
        f"profile={profile_name}, protocol={protocol}, path={path}, status={resp.status_code}, body={resp.text[:400]}"
    )
    assert resp.status_code < 500, (
        f"profile={profile_name}, protocol={protocol}, path={path}, status={resp.status_code}, body={resp.text[:400]}"
    )

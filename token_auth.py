import asyncio
import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx

# 与 Codex OAuth 兼容的 OpenAI OAuth 参数
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
STORE_FILENAME = ".codex_oauth.json"

# 刷新提前量，避免边界时刻 token 刚好过期
REFRESH_MARGIN_SECONDS = 60
# device 授权轮询超时时间（秒）
PENDING_TTL_SECONDS = 10 * 60
OAUTH_POLLING_SAFETY_MARGIN_SECONDS = 3

_lock = asyncio.Lock()


def get_x_auth_token(*args, **kwargs):
    return None


def _now_ts() -> int:
    return int(time.time())


def _store_path() -> Path:
    custom = os.getenv("CODEX_OAUTH_STORE_PATH", "").strip()
    if custom:
        return Path(custom)
    return Path(STORE_FILENAME)


def _read_store() -> Dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 文件损坏时按空处理，避免服务直接崩溃
        return {}


def _write_store(data: Dict[str, Any]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
        return None
    except Exception:
        return None


def _extract_account_id_from_claims(claims: Dict[str, Any]) -> Optional[str]:
    direct = claims.get("chatgpt_account_id")
    if isinstance(direct, str) and direct:
        return direct

    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        via_auth = auth_claim.get("chatgpt_account_id")
        if isinstance(via_auth, str) and via_auth:
            return via_auth

    orgs = claims.get("organizations")
    if isinstance(orgs, list) and orgs:
        first = orgs[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id
    return None


def _extract_account_id(tokens: Dict[str, Any]) -> Optional[str]:
    for key in ("id_token", "access_token"):
        val = tokens.get(key)
        if isinstance(val, str) and val:
            claims = _parse_jwt_claims(val)
            if claims:
                account_id = _extract_account_id_from_claims(claims)
                if account_id:
                    return account_id
    return None


async def _exchange_code_for_tokens(code: str, redirect_uri: str, code_verifier: str) -> Dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{ISSUER}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


async def _refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{ISSUER}/oauth/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=payload,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"token refresh failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


async def _save_oauth_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")
    expires_in = int(tokens.get("expires_in") or 3600)
    if not access_token or not refresh_token:
        raise RuntimeError("OAuth 返回缺少 access_token 或 refresh_token")

    account_id = _extract_account_id(tokens) or ""
    record = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _now_ts() + expires_in,
        "account_id": account_id,
        "updated_at": _now_ts(),
    }
    async with _lock:
        data = _read_store()
        data["codex_oauth"] = record
        _write_store(data)
    return record


async def start_codex_device_oauth() -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{ISSUER}/api/accounts/deviceauth/usercode",
            headers={"Content-Type": "application/json", "User-Agent": "llm_proxy/1.0"},
            json={"client_id": CLIENT_ID},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"启动 device auth 失败: {resp.status_code} {resp.text[:300]}")
    payload = resp.json()
    interval_raw = str(payload.get("interval") or "5")
    try:
        interval_seconds = max(int(interval_raw), 1)
    except Exception:
        interval_seconds = 5
    return {
        "device_auth_id": str(payload.get("device_auth_id") or ""),
        "user_code": str(payload.get("user_code") or ""),
        "verification_url": f"{ISSUER}/codex/device",
        "interval_seconds": interval_seconds,
    }


async def finish_codex_device_oauth(device_auth_id: str, user_code: str, interval_seconds: int) -> Dict[str, Any]:
    if not device_auth_id or not user_code:
        raise ValueError("device_auth_id 或 user_code 为空")

    poll_interval = max(int(interval_seconds or 5), 1) + OAUTH_POLLING_SAFETY_MARGIN_SECONDS
    deadline = _now_ts() + PENDING_TTL_SECONDS

    while _now_ts() < deadline:
        async with httpx.AsyncClient(timeout=30.0) as client:
            poll_resp = await client.post(
                f"{ISSUER}/api/accounts/deviceauth/token",
                headers={"Content-Type": "application/json", "User-Agent": "llm_proxy/1.0"},
                json={"device_auth_id": device_auth_id, "user_code": user_code},
            )

        if poll_resp.status_code == 200:
            device_token = poll_resp.json()
            authorization_code = str(device_token.get("authorization_code") or "")
            code_verifier = str(device_token.get("code_verifier") or "")
            if not authorization_code or not code_verifier:
                raise RuntimeError("device auth 返回缺少 authorization_code/code_verifier")
            tokens = await _exchange_code_for_tokens(
                code=authorization_code,
                redirect_uri=f"{ISSUER}/deviceauth/callback",
                code_verifier=code_verifier,
            )
            record = await _save_oauth_tokens(tokens)
            return {
                "expires_at": int(record.get("expires_at") or 0),
                "account_id": str(record.get("account_id") or ""),
            }

        # 403 / 404 表示用户还未确认，继续轮询
        if poll_resp.status_code not in {403, 404}:
            raise RuntimeError(f"device auth 轮询失败: {poll_resp.status_code} {poll_resp.text[:300]}")

        await asyncio.sleep(poll_interval)

    raise TimeoutError("Codex 登录超时，请重新执行 start_proxy.py")


async def ensure_codex_login_for_startup() -> bool:
    """
    启动前检查 Codex OAuth；若未登录则走阻塞式 device flow 登录。
    返回值：
    - True: 本次执行了登录流程
    - False: 已有可用凭证，无需登录
    """
    status = await get_codex_auth_status()
    if status.get("authorized"):
        return False

    start = await start_codex_device_oauth()
    code = start["user_code"]
    url = start["verification_url"]
    print("\n[Codex OAuth] 检测到未登录，启动登录流程...")
    print(f"[Codex OAuth] 1) 打开浏览器访问: {url}")
    print(f"[Codex OAuth] 2) 输入验证码: {code}")
    print("[Codex OAuth] 3) 完成确认后，终端会自动继续启动服务\n")

    await finish_codex_device_oauth(
        device_auth_id=start["device_auth_id"],
        user_code=start["user_code"],
        interval_seconds=start["interval_seconds"],
    )
    print("[Codex OAuth] 登录成功，继续启动服务。")
    return True


async def get_codex_auth_status() -> Dict[str, Any]:
    env_access = os.getenv("CODEX_ACCESS_TOKEN", "").strip()
    env_account = os.getenv("CODEX_ACCOUNT_ID", "").strip()
    if env_access:
        return {
            "authorized": True,
            "source": "env",
            "account_id": env_account,
            "expires_at": None,
        }

    root = _read_store()
    data = root.get("codex_oauth") if isinstance(root, dict) else None
    if not isinstance(data, dict):
        return {
            "authorized": False,
            "source": "none",
            "account_id": "",
            "expires_at": None,
        }

    expires_at = int(data.get("expires_at") or 0)
    refresh_token = str(data.get("refresh_token") or "")
    still_usable = expires_at > _now_ts() or bool(refresh_token)
    return {
        "authorized": still_usable,
        "source": "store",
        "account_id": str(data.get("account_id") or ""),
        "expires_at": expires_at or None,
    }


async def _get_or_refresh_store_token() -> Tuple[str, str]:
    async with _lock:
        data = _read_store()
        record = data.get("codex_oauth") if isinstance(data, dict) else None
        if not isinstance(record, dict):
            raise RuntimeError("未完成 Codex OAuth 登录，请先执行 start_proxy.py 完成登录")
        access_token = str(record.get("access_token") or "")
        refresh_token = str(record.get("refresh_token") or "")
        expires_at = int(record.get("expires_at") or 0)
        account_id = str(record.get("account_id") or "")

    if not refresh_token:
        raise RuntimeError("本地 Codex 凭证缺少 refresh_token，请重新登录")

    if access_token and expires_at > (_now_ts() + REFRESH_MARGIN_SECONDS):
        return access_token, account_id

    refreshed = await _refresh_access_token(refresh_token)
    new_access = str(refreshed.get("access_token") or "")
    new_refresh = str(refreshed.get("refresh_token") or refresh_token)
    expires_in = int(refreshed.get("expires_in") or 3600)
    if not new_access:
        raise RuntimeError("刷新 token 失败：返回缺少 access_token")
    new_account_id = _extract_account_id(refreshed) or account_id

    async with _lock:
        latest = _read_store()
        latest_record = latest.get("codex_oauth") if isinstance(latest, dict) else None
        if not isinstance(latest_record, dict):
            raise RuntimeError("本地 Codex 凭证已失效，请重新登录")
        latest_record["access_token"] = new_access
        latest_record["refresh_token"] = new_refresh
        latest_record["expires_at"] = _now_ts() + expires_in
        latest_record["account_id"] = new_account_id
        latest_record["updated_at"] = _now_ts()
        latest["codex_oauth"] = latest_record
        _write_store(latest)

    return new_access, new_account_id


async def get_codex_upstream_headers(profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    auth = {}
    if isinstance(profile, dict):
        maybe_auth = profile.get("auth")
        if isinstance(maybe_auth, dict):
            auth = maybe_auth

    access_env_name = str(auth.get("accessTokenEnv") or "CODEX_ACCESS_TOKEN")
    account_env_name = str(auth.get("accountIdEnv") or "CODEX_ACCOUNT_ID")

    # 兼容显式环境变量注入，便于无状态容器部署
    env_access = os.getenv(access_env_name, "").strip()
    env_account = os.getenv(account_env_name, "").strip()
    if env_access:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {env_access}",
        }
        if env_account:
            headers["ChatGPT-Account-Id"] = env_account
        return headers

    access_token, account_id = await _get_or_refresh_store_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return headers


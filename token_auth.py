import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
OAUTH_CALLBACK_PORT = 1455
OAUTH_CALLBACK_PATH = "/auth/callback"
OAUTH_SCOPES = "openid profile email offline_access"

POOL_SCHEMA_VERSION = 2
DEFAULT_LABEL = "primary"
DEFAULT_PRIORITY = 100
DEFAULT_COOLDOWN_SECONDS = 300
DEFAULT_MAX_FAILOVER_PER_REQUEST = 2
RATE_LIMIT_STATUS_CODES = {406, 429}
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

_lock = asyncio.Lock()


class CodexAccountUnavailableError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, error_type: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


def get_x_auth_token(*args, **kwargs):
    return None


def _now_ts() -> int:
    return int(time.time())


def _store_path() -> Path:
    return Path(STORE_FILENAME)


def _read_store() -> Dict[str, Any]:
    path = _store_path()
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
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


def _label_key(label: str) -> str:
    return label.strip().lower()


def _validate_label(label: str) -> str:
    clean = label.strip()
    if not LABEL_PATTERN.fullmatch(clean):
        raise ValueError("label 非法：仅允许 A-Z/a-z/0-9/./_/-，长度 1-32")
    return clean


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _normalize_account(raw: Dict[str, Any], fallback_label: str) -> Optional[Dict[str, Any]]:
    label_raw = str(raw.get("label") or fallback_label).strip()
    if not label_raw:
        return None
    if not LABEL_PATTERN.fullmatch(label_raw):
        return None

    return {
        "label": label_raw,
        "account_id": str(raw.get("account_id") or ""),
        "priority": _as_int(raw.get("priority"), DEFAULT_PRIORITY),
        "enabled": _as_bool(raw.get("enabled"), True),
        "access_token": str(raw.get("access_token") or ""),
        "refresh_token": str(raw.get("refresh_token") or ""),
        "expires_at": _as_int(raw.get("expires_at"), 0),
        "cooldown_until": _as_int(raw.get("cooldown_until"), 0),
        "last_error": str(raw.get("last_error") or ""),
        "updated_at": _as_int(raw.get("updated_at"), _now_ts()),
    }


def _normalize_store(raw_root: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    root = dict(raw_root) if isinstance(raw_root, dict) else {}
    changed = False
    accounts: List[Dict[str, Any]] = []

    if isinstance(root.get("schema_version"), int) and int(root.get("schema_version")) == POOL_SCHEMA_VERSION:
        raw_accounts = root.get("accounts")
        if not isinstance(raw_accounts, list):
            raw_accounts = []
            changed = True
        seen = set()
        for idx, item in enumerate(raw_accounts):
            if not isinstance(item, dict):
                changed = True
                continue
            normalized = _normalize_account(item, fallback_label=f"account-{idx+1}")
            if not normalized:
                changed = True
                continue
            key = _label_key(normalized["label"])
            if key in seen:
                changed = True
                continue
            seen.add(key)
            accounts.append(normalized)
    else:
        legacy = root.get("codex_oauth")
        if isinstance(legacy, dict):
            # 兼容旧格式：单账号迁移到 primary。
            migrated = _normalize_account({"label": DEFAULT_LABEL, **legacy}, fallback_label=DEFAULT_LABEL)
            if migrated:
                accounts = [migrated]
                changed = True

    default_label = str(root.get("default_label") or "").strip()
    if accounts:
        label_keys = {_label_key(item["label"]): item["label"] for item in accounts}
        if _label_key(default_label) not in label_keys:
            default_label = sorted(accounts, key=lambda it: (int(it.get("priority") or DEFAULT_PRIORITY), it["label"]))[0]["label"]
            changed = True
        else:
            canonical = label_keys[_label_key(default_label)]
            if canonical != default_label:
                default_label = canonical
                changed = True
    else:
        if default_label:
            default_label = ""
            changed = True

    normalized_root = dict(root)
    normalized_root["schema_version"] = POOL_SCHEMA_VERSION
    normalized_root["default_label"] = default_label
    normalized_root["accounts"] = accounts
    if "codex_oauth" in normalized_root:
        normalized_root.pop("codex_oauth", None)
        changed = True

    if normalized_root != root:
        changed = True

    return normalized_root, changed


def _load_pool_locked() -> Dict[str, Any]:
    root = _read_store()
    normalized, changed = _normalize_store(root)
    if changed:
        _write_store(normalized)
    return normalized


def _policy_from_profile(profile: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    cooldown_seconds = DEFAULT_COOLDOWN_SECONDS
    max_failover = DEFAULT_MAX_FAILOVER_PER_REQUEST
    if not isinstance(profile, dict):
        return cooldown_seconds, max_failover

    auth = profile.get("auth")
    if not isinstance(auth, dict):
        return cooldown_seconds, max_failover

    pool_policy = auth.get("accountPoolPolicy")
    if not isinstance(pool_policy, dict):
        return cooldown_seconds, max_failover

    cooldown_seconds = max(1, _as_int(pool_policy.get("cooldownSeconds"), DEFAULT_COOLDOWN_SECONDS))
    max_failover = max(0, _as_int(pool_policy.get("maxFailoverPerRequest"), DEFAULT_MAX_FAILOVER_PER_REQUEST))
    return cooldown_seconds, max_failover


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _generate_pkce() -> Tuple[str, str]:
    verifier = _base64url_encode(secrets.token_bytes(32))
    challenge = _base64url_encode(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _generate_state() -> str:
    return _base64url_encode(secrets.token_bytes(24))


def _build_browser_authorize_url(redirect_uri: str, code_challenge: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": OAUTH_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
            "originator": "llm_proxy",
        }
    )
    return f"{ISSUER}/oauth/authorize?{query}"


def _html_success() -> str:
    return """<!doctype html><html><head><title>Codex OAuth Success</title></head><body>
<h2>Authorization Successful</h2><p>You can close this window and return to terminal.</p>
<script>setTimeout(() => window.close(), 1500)</script></body></html>"""


def _html_error(message: str) -> str:
    escaped = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"""<!doctype html><html><head><title>Codex OAuth Failed</title></head><body>
<h2>Authorization Failed</h2><pre>{escaped}</pre></body></html>"""


class _OAuthCallbackState:
    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.event = threading.Event()
        self.code = ""
        self.error = ""


def _run_browser_oauth_callback_server(expected_state: str, timeout_seconds: int = PENDING_TTL_SECONDS) -> str:
    callback_state = _OAuthCallbackState(expected_state)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _write_html(self, body: str, status: int = 200) -> None:
            data = body.encode("utf-8", errors="replace")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != OAUTH_CALLBACK_PATH:
                self._write_html(_html_error("Not found"), status=404)
                return

            q = urllib.parse.parse_qs(parsed.query)
            code = str((q.get("code") or [""])[0] or "")
            state = str((q.get("state") or [""])[0] or "")
            error = str((q.get("error") or [""])[0] or "")
            error_description = str((q.get("error_description") or [""])[0] or "")

            if error:
                callback_state.error = error_description or error
                callback_state.event.set()
                self._write_html(_html_error(callback_state.error), status=200)
                return

            if not code:
                callback_state.error = "Missing authorization code"
                callback_state.event.set()
                self._write_html(_html_error(callback_state.error), status=400)
                return

            if state != callback_state.expected_state:
                callback_state.error = "Invalid OAuth state"
                callback_state.event.set()
                self._write_html(_html_error(callback_state.error), status=400)
                return

            callback_state.code = code
            callback_state.event.set()
            self._write_html(_html_success(), status=200)

    server = ThreadingHTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not callback_state.event.wait(timeout_seconds):
            raise TimeoutError("OAuth callback timeout")
        if callback_state.error:
            raise RuntimeError(callback_state.error)
        if not callback_state.code:
            raise RuntimeError("OAuth callback missing authorization code")
        return callback_state.code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


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


async def _save_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    label: Optional[str] = None,
    priority: Optional[int] = None,
    enabled: bool = True,
    set_default: bool = False,
) -> Dict[str, Any]:
    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")
    expires_in = int(tokens.get("expires_in") or 3600)
    if not access_token or not refresh_token:
        raise RuntimeError("OAuth 返回缺少 access_token 或 refresh_token")

    account_id = _extract_account_id(tokens) or ""
    now = _now_ts()
    target_label = _validate_label(label or DEFAULT_LABEL)

    async with _lock:
        root = _load_pool_locked()
        accounts = root.get("accounts") or []
        if not isinstance(accounts, list):
            accounts = []

        idx = -1
        for i, item in enumerate(accounts):
            if isinstance(item, dict) and _label_key(str(item.get("label") or "")) == _label_key(target_label):
                idx = i
                break

        if idx < 0:
            record = {
                "label": target_label,
                "account_id": account_id,
                "priority": int(priority if priority is not None else DEFAULT_PRIORITY),
                "enabled": bool(enabled),
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": now + expires_in,
                "cooldown_until": 0,
                "last_error": "",
                "updated_at": now,
            }
            accounts.append(record)
        else:
            record = dict(accounts[idx])
            record["label"] = target_label
            record["account_id"] = account_id or str(record.get("account_id") or "")
            record["priority"] = int(priority if priority is not None else _as_int(record.get("priority"), DEFAULT_PRIORITY))
            record["enabled"] = bool(enabled if enabled is not None else _as_bool(record.get("enabled"), True))
            record["access_token"] = access_token
            record["refresh_token"] = refresh_token
            record["expires_at"] = now + expires_in
            record["cooldown_until"] = 0
            record["last_error"] = ""
            record["updated_at"] = now
            accounts[idx] = record

        root["accounts"] = accounts
        if set_default or (not str(root.get("default_label") or "").strip()):
            root["default_label"] = target_label
        _write_store(root)
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


async def start_codex_browser_oauth() -> Dict[str, Any]:
    code_verifier, code_challenge = _generate_pkce()
    state = _generate_state()
    redirect_uri = f"http://localhost:{OAUTH_CALLBACK_PORT}{OAUTH_CALLBACK_PATH}"
    authorize_url = _build_browser_authorize_url(
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        state=state,
    )
    return {
        "authorize_url": authorize_url,
        "state": state,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
    }


async def _finish_codex_browser_oauth(state: str, redirect_uri: str, code_verifier: str) -> Dict[str, Any]:
    code = await asyncio.to_thread(_run_browser_oauth_callback_server, state, PENDING_TTL_SECONDS)
    return await _exchange_code_for_tokens(
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )


async def _poll_device_auth_for_tokens(device_auth_id: str, user_code: str, interval_seconds: int) -> Dict[str, Any]:
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
            return await _exchange_code_for_tokens(
                code=authorization_code,
                redirect_uri=f"{ISSUER}/deviceauth/callback",
                code_verifier=code_verifier,
            )

        # 403 / 404 表示用户还未确认，继续轮询
        if poll_resp.status_code not in {403, 404}:
            raise RuntimeError(f"device auth 轮询失败: {poll_resp.status_code} {poll_resp.text[:300]}")

        await asyncio.sleep(poll_interval)

    raise TimeoutError("Codex 登录超时，请重新执行 start_proxy.py")


async def finish_codex_device_oauth(device_auth_id: str, user_code: str, interval_seconds: int) -> Dict[str, Any]:
    tokens = await _poll_device_auth_for_tokens(device_auth_id=device_auth_id, user_code=user_code, interval_seconds=interval_seconds)
    record = await _save_oauth_tokens(tokens=tokens, label=DEFAULT_LABEL, set_default=True)
    return {
        "label": str(record.get("label") or DEFAULT_LABEL),
        "expires_at": int(record.get("expires_at") or 0),
        "account_id": str(record.get("account_id") or ""),
    }


async def add_codex_account_via_device_oauth(label: str, *, priority: int = DEFAULT_PRIORITY) -> Dict[str, Any]:
    clean = _validate_label(label)
    async with _lock:
        root = _load_pool_locked()
        for item in root.get("accounts") or []:
            if isinstance(item, dict) and _label_key(str(item.get("label") or "")) == _label_key(clean):
                raise RuntimeError(f"label 已存在: {clean}")

    start = await start_codex_device_oauth()
    print("\n[Codex OAuth] 添加账号流程...")
    print(f"[Codex OAuth] label: {clean}")
    print(f"[Codex OAuth] 1) 打开浏览器访问: {start['verification_url']}")
    print(f"[Codex OAuth] 2) 输入验证码: {start['user_code']}")
    print("[Codex OAuth] 3) 完成确认后，终端会自动继续\n")

    tokens = await _poll_device_auth_for_tokens(
        device_auth_id=start["device_auth_id"],
        user_code=start["user_code"],
        interval_seconds=start["interval_seconds"],
    )
    record = await _save_oauth_tokens(
        tokens=tokens,
        label=clean,
        priority=priority,
        enabled=True,
        set_default=False,
    )
    return _public_account_view(record, default_label="")


async def add_codex_account_via_browser_oauth(label: str, *, priority: int = DEFAULT_PRIORITY) -> Dict[str, Any]:
    clean = _validate_label(label)
    async with _lock:
        root = _load_pool_locked()
        for item in root.get("accounts") or []:
            if isinstance(item, dict) and _label_key(str(item.get("label") or "")) == _label_key(clean):
                raise RuntimeError(f"label 已存在: {clean}")

    start = await start_codex_browser_oauth()
    print("\n[Codex OAuth] Browser 登录流程...")
    print(f"[Codex OAuth] label: {clean}")
    print(f"[Codex OAuth] 回调地址: {start['redirect_uri']}")
    print(f"[Codex OAuth] 请在浏览器完成授权: {start['authorize_url']}\n")
    try:
        webbrowser.open(start["authorize_url"])
    except Exception:
        pass

    tokens = await _finish_codex_browser_oauth(
        state=start["state"],
        redirect_uri=start["redirect_uri"],
        code_verifier=start["code_verifier"],
    )
    record = await _save_oauth_tokens(
        tokens=tokens,
        label=clean,
        priority=priority,
        enabled=True,
        set_default=False,
    )
    return _public_account_view(record, default_label="")


def _public_account_view(record: Dict[str, Any], *, default_label: str) -> Dict[str, Any]:
    label = str(record.get("label") or "")
    return {
        "label": label,
        "account_id": str(record.get("account_id") or ""),
        "priority": _as_int(record.get("priority"), DEFAULT_PRIORITY),
        "enabled": _as_bool(record.get("enabled"), True),
        "expires_at": _as_int(record.get("expires_at"), 0),
        "cooldown_until": _as_int(record.get("cooldown_until"), 0),
        "updated_at": _as_int(record.get("updated_at"), 0),
        "last_error": str(record.get("last_error") or ""),
        "is_default": _label_key(label) == _label_key(default_label),
    }


async def list_codex_accounts() -> Dict[str, Any]:
    async with _lock:
        root = _load_pool_locked()
        default_label = str(root.get("default_label") or "")
        accounts = root.get("accounts") or []
        out = []
        for item in sorted(
            [it for it in accounts if isinstance(it, dict)],
            key=lambda it: (_as_int(it.get("priority"), DEFAULT_PRIORITY), str(it.get("label") or "")),
        ):
            out.append(_public_account_view(item, default_label=default_label))
        return {
            "default_label": default_label,
            "accounts": out,
        }


async def set_codex_account_enabled(label: str, enabled: bool) -> Dict[str, Any]:
    clean = _validate_label(label)
    async with _lock:
        root = _load_pool_locked()
        default_label = str(root.get("default_label") or "")
        accounts = root.get("accounts") or []
        idx = -1
        for i, item in enumerate(accounts):
            if isinstance(item, dict) and _label_key(str(item.get("label") or "")) == _label_key(clean):
                idx = i
                break
        if idx < 0:
            raise RuntimeError(f"label 不存在: {clean}")

        record = dict(accounts[idx])
        record["enabled"] = bool(enabled)
        record["updated_at"] = _now_ts()
        if not enabled:
            record["cooldown_until"] = 0
        accounts[idx] = record
        root["accounts"] = accounts
        _write_store(root)
        return _public_account_view(record, default_label=default_label)


async def switch_codex_default_account(label: str) -> Dict[str, Any]:
    clean = _validate_label(label)
    async with _lock:
        root = _load_pool_locked()
        accounts = root.get("accounts") or []
        matched = None
        for item in accounts:
            if isinstance(item, dict) and _label_key(str(item.get("label") or "")) == _label_key(clean):
                matched = item
                break
        if not isinstance(matched, dict):
            raise RuntimeError(f"label 不存在: {clean}")

        root["default_label"] = str(matched.get("label") or clean)
        _write_store(root)
        return _public_account_view(matched, default_label=str(root.get("default_label") or ""))


async def remove_codex_account(label: str) -> Dict[str, Any]:
    clean = _validate_label(label)
    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        kept = []
        removed = None
        for item in accounts:
            if _label_key(str(item.get("label") or "")) == _label_key(clean):
                removed = item
                continue
            kept.append(item)

        if removed is None:
            raise RuntimeError(f"label 不存在: {clean}")

        root["accounts"] = kept
        if _label_key(str(root.get("default_label") or "")) == _label_key(str(removed.get("label") or "")):
            if kept:
                next_default = sorted(kept, key=lambda it: (_as_int(it.get("priority"), DEFAULT_PRIORITY), str(it.get("label") or "")))[0]
                root["default_label"] = str(next_default.get("label") or "")
            else:
                root["default_label"] = ""
        _write_store(root)
        return _public_account_view(removed, default_label=str(root.get("default_label") or ""))


def _find_account_index_by_label(accounts: List[Dict[str, Any]], label: str) -> int:
    target = _label_key(label)
    for idx, item in enumerate(accounts):
        if _label_key(str(item.get("label") or "")) == target:
            return idx
    return -1


def _find_account_index_by_headers(accounts: List[Dict[str, Any]], headers: Dict[str, str]) -> int:
    lowered_headers = {str(k).lower(): str(v or "") for k, v in headers.items()}
    account_id = str(lowered_headers.get("chatgpt-account-id") or "").strip()
    auth = str(lowered_headers.get("authorization") or "").strip()
    access_token = ""
    if auth.lower().startswith("bearer "):
        access_token = auth[7:].strip()

    if account_id:
        for idx, item in enumerate(accounts):
            if str(item.get("account_id") or "").strip() == account_id:
                return idx

    if access_token:
        for idx, item in enumerate(accounts):
            if str(item.get("access_token") or "") == access_token:
                return idx

    return -1


async def _get_or_refresh_account_token(
    label: str,
    *,
    cooldown_seconds: int,
    apply_cooldown_on_refresh_failure: bool,
) -> Tuple[str, str]:
    clean = _validate_label(label)

    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        idx = _find_account_index_by_label(accounts, clean)
        if idx < 0:
            raise RuntimeError(f"账号不存在: {clean}")

        record = dict(accounts[idx])
        access_token = str(record.get("access_token") or "")
        refresh_token = str(record.get("refresh_token") or "")
        expires_at = _as_int(record.get("expires_at"), 0)
        account_id = str(record.get("account_id") or "")

    if not refresh_token:
        raise RuntimeError(f"账号 {clean} 缺少 refresh_token，请重新登录")

    if access_token and expires_at > (_now_ts() + REFRESH_MARGIN_SECONDS):
        return access_token, account_id

    try:
        refreshed = await _refresh_access_token(refresh_token)
    except Exception as e:
        if apply_cooldown_on_refresh_failure:
            await mark_codex_account_rate_limited(
                headers={"Authorization": f"Bearer {access_token}", "ChatGPT-Account-Id": account_id},
                status_code=429,
                error_text=f"refresh_failed: {e}",
                profile={"auth": {"accountPoolPolicy": {"cooldownSeconds": cooldown_seconds}}},
            )
        raise

    new_access = str(refreshed.get("access_token") or "")
    new_refresh = str(refreshed.get("refresh_token") or refresh_token)
    expires_in = int(refreshed.get("expires_in") or 3600)
    if not new_access:
        raise RuntimeError(f"账号 {clean} 刷新 token 失败：返回缺少 access_token")
    new_account_id = _extract_account_id(refreshed) or account_id

    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        idx = _find_account_index_by_label(accounts, clean)
        if idx < 0:
            raise RuntimeError(f"账号不存在: {clean}")
        latest = dict(accounts[idx])
        latest["access_token"] = new_access
        latest["refresh_token"] = new_refresh
        latest["expires_at"] = _now_ts() + expires_in
        latest["account_id"] = new_account_id
        latest["updated_at"] = _now_ts()
        accounts[idx] = latest
        root["accounts"] = accounts
        _write_store(root)

    return new_access, new_account_id


async def get_codex_auth_status() -> Dict[str, Any]:
    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        enabled = [it for it in accounts if _as_bool(it.get("enabled"), True)]

    now = _now_ts()
    usable = 0
    for item in enabled:
        expires_at = _as_int(item.get("expires_at"), 0)
        refresh_token = str(item.get("refresh_token") or "")
        cooldown_until = _as_int(item.get("cooldown_until"), 0)
        if cooldown_until > now:
            continue
        if expires_at > now or bool(refresh_token):
            usable += 1

    return {
        "authorized": usable > 0,
        "source": "store",
        "default_label": str(root.get("default_label") or ""),
        "total_accounts": len(accounts),
        "enabled_accounts": len(enabled),
        "usable_accounts": usable,
    }


async def _select_candidate_labels(profile: Optional[Dict[str, Any]]) -> List[str]:
    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        if not accounts:
            raise CodexAccountUnavailableError(
                "未配置 Codex 账号，请先执行: python manage_codex_accounts.py",
                status_code=503,
                error_type="service_unavailable",
            )

        enabled_accounts = [it for it in accounts if _as_bool(it.get("enabled"), True)]
        if not enabled_accounts:
            raise CodexAccountUnavailableError(
                "Codex 账号池为空或均被禁用，请先启用至少一个账号",
                status_code=503,
                error_type="service_unavailable",
            )

        now = _now_ts()
        available = [it for it in enabled_accounts if _as_int(it.get("cooldown_until"), 0) <= now]
        if not available:
            nearest = min(_as_int(it.get("cooldown_until"), now) for it in enabled_accounts)
            wait_seconds = max(0, nearest - now)
            raise CodexAccountUnavailableError(
                f"所有 Codex 账号均在冷却中，请等待约 {wait_seconds}s",
                status_code=429,
                error_type="rate_limit_error",
            )

        ordered = sorted(available, key=lambda it: (_as_int(it.get("priority"), DEFAULT_PRIORITY), str(it.get("label") or "")))
        return [str(item.get("label") or "") for item in ordered if str(item.get("label") or "")]


async def get_codex_upstream_headers(profile: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    cooldown_seconds, _ = _policy_from_profile(profile)
    labels = await _select_candidate_labels(profile)

    last_error: Optional[Exception] = None
    for label in labels:
        try:
            access_token, account_id = await _get_or_refresh_account_token(
                label,
                cooldown_seconds=cooldown_seconds,
                apply_cooldown_on_refresh_failure=True,
            )
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            if account_id:
                headers["ChatGPT-Account-Id"] = account_id
            return headers
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"没有可用的 Codex 账号: {last_error}")


async def mark_codex_account_rate_limited(
    *,
    headers: Dict[str, str],
    status_code: int,
    error_text: str,
    profile: Optional[Dict[str, Any]] = None,
) -> None:
    # TODO: 后续根据线上真实 error body 精细化触发条件；当前仅 406/429 会触发切换。
    if status_code not in RATE_LIMIT_STATUS_CODES:
        return

    cooldown_seconds, _ = _policy_from_profile(profile)
    async with _lock:
        root = _load_pool_locked()
        accounts = [it for it in root.get("accounts") or [] if isinstance(it, dict)]
        idx = _find_account_index_by_headers(accounts, headers)
        if idx < 0:
            return

        record = dict(accounts[idx])
        record["cooldown_until"] = _now_ts() + cooldown_seconds
        record["last_error"] = f"{status_code}: {str(error_text or '')[:300]}"
        record["updated_at"] = _now_ts()
        accounts[idx] = record
        root["accounts"] = accounts
        _write_store(root)


async def login_all_codex_accounts() -> Dict[str, Any]:
    status = await list_codex_accounts()
    accounts = status.get("accounts") or []
    ok = 0
    failed = 0
    details = []

    for item in accounts:
        label = str(item.get("label") or "")
        enabled = bool(item.get("enabled"))
        if not label or not enabled:
            continue
        try:
            await _get_or_refresh_account_token(
                label,
                cooldown_seconds=DEFAULT_COOLDOWN_SECONDS,
                apply_cooldown_on_refresh_failure=False,
            )
            ok += 1
            details.append({"label": label, "ok": True})
        except Exception as e:
            failed += 1
            details.append({"label": label, "ok": False, "error": str(e)})

    return {
        "ok": ok,
        "failed": failed,
        "details": details,
    }


async def ensure_codex_login_for_startup(login_method: Optional[str] = None) -> bool:
    """
    启动前检查 Codex OAuth；若未登录则走阻塞式 device flow 登录。
    返回值：
    - True: 本次执行了登录流程
    - False: 已有可用凭证，无需登录
    """
    _ = login_method
    status = await get_codex_auth_status()
    if not status.get("authorized"):
        raise RuntimeError(
            "Codex 账号池无可用账号。请先运行: python manage_codex_accounts.py （进入交互式向导添加账号）"
        )

    summary = await login_all_codex_accounts()
    if int(summary.get("ok") or 0) <= 0:
        raise RuntimeError(
            "Codex 账号池无可用账号。请先运行: python manage_codex_accounts.py （进入交互式向导添加账号）"
        )

    return False

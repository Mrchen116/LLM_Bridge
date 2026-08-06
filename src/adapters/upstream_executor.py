from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List, Mapping, Optional

import httpx
import zstandard as zstd

from token_auth import (
    get_codex_upstream_headers,
    get_x_auth_token,
    mark_codex_account_auth_expired,
    mark_codex_account_rate_limited,
)
from upstream_config import (
    build_auth_headers,
    get_effective_auth_type,
    is_request_compression_enabled,
)

RATE_LIMIT_STATUS_CODES = {406, 429}
# 401/403 多为 token 过期或认证失效，需刷新 token 后重试，不应对账号冷却
CODEX_AUTH_STATUS_CODES = {401, 403}
CODEX_FAILOVER_ERROR_CODES = {
    "insufficient_quota",
    "usage_not_included",
    "deactivated_workspace",
}
CODEX_RETRYABLE_ERROR_CODES = CODEX_FAILOVER_ERROR_CODES | {
    "server_error",
}
CODEX_DEFAULT_ORIGINATOR = "codex_cli_rs"
CODEX_DEFAULT_BETA_FEATURES = "multi_agent,prevent_idle_sleep"
CODEX_DEFAULT_VERSION = "0.144.6"
CODEX_UPSTREAM_CLIENT_VERSION_ENV = "CODEX_UPSTREAM_CLIENT_VERSION"


def _get_codex_upstream_client_version() -> str:
    configured = os.getenv(CODEX_UPSTREAM_CLIENT_VERSION_ENV, "").strip()
    return configured or CODEX_DEFAULT_VERSION


def is_rate_limit_status(status_code: int) -> bool:
    return status_code in RATE_LIMIT_STATUS_CODES


def _extract_error_code_from_text(error_text: str) -> str:
    text = str(error_text or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        obj = None

    if isinstance(obj, dict):
        if isinstance(obj.get("code"), str):
            return str(obj.get("code") or "").strip().lower()
        err = obj.get("error")
        if isinstance(err, dict):
            if isinstance(err.get("code"), str):
                return str(err.get("code") or "").strip().lower()
            # 上游偶发把真实 JSON 塞进 error.message，需再解一层
            msg = err.get("message")
            if isinstance(msg, str):
                msg_stripped = msg.strip()
                if msg_stripped.startswith("{"):
                    nested = _extract_error_code_from_text(msg_stripped)
                    if nested:
                        return nested
            if isinstance(err.get("type"), str):
                return str(err.get("type") or "").strip().lower()
        if isinstance(err, str):
            return err.strip().lower()
        detail = obj.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            return str(detail.get("code") or "").strip().lower()

    lowered = text.lower()
    for code in CODEX_FAILOVER_ERROR_CODES:
        if code in lowered:
            return code
    return ""


def should_trigger_codex_failover(status_code: int, error_text: str = "") -> bool:
    # TODO: 后续根据线上真实 error body 精细化触发条件。
    if status_code in CODEX_AUTH_STATUS_CODES:
        return True
    if is_rate_limit_status(status_code):
        return True
    code = _extract_error_code_from_text(error_text)
    return code in CODEX_FAILOVER_ERROR_CODES


def should_mark_codex_cooldown(status_code: int, error_text: str = "") -> bool:
    """401/403 若带 insufficient_quota 等则为限流类，应冷却；空 body 或纯认证错误则不冷却。"""
    if status_code in CODEX_AUTH_STATUS_CODES:
        code = _extract_error_code_from_text(error_text)
        if code in CODEX_FAILOVER_ERROR_CODES:
            return True  # 403 + insufficient_quota 等 = 限流，需冷却
        return False  # 403 空 body 或未知 = 当 token 过期，刷新重试即可
    return should_trigger_codex_failover(status_code, error_text)


def should_retry_codex_result(status_code: int, error_text: str = "") -> bool:
    if status_code >= 500:
        return True
    if status_code in CODEX_AUTH_STATUS_CODES:
        return True
    if is_rate_limit_status(status_code):
        return True
    code = _extract_error_code_from_text(error_text)
    return code in CODEX_RETRYABLE_ERROR_CODES


def _header_value_case_insensitive(headers: Optional[Mapping[str, Any]], key: str) -> str:
    if not headers:
        return ""
    key_lower = key.lower()
    for raw_k, raw_v in headers.items():
        if str(raw_k).lower() != key_lower:
            continue
        value = str(raw_v or "").strip()
        if value:
            return value
    return ""


def build_upstream_request_kwargs(
    *,
    profile: Dict[str, Any],
    headers: Mapping[str, Any],
    request_body: Dict[str, Any],
) -> tuple[Dict[str, str], Dict[str, Any]]:
    request_headers = {str(k): str(v) for k, v in headers.items()}
    auth_type = get_effective_auth_type(profile)
    should_compress = auth_type == "codex_oauth" and is_request_compression_enabled(profile)
    if not should_compress:
        return request_headers, {"json": request_body}

    if _header_value_case_insensitive(request_headers, "content-encoding"):
        raise ValueError("request compression was requested but content-encoding is already set")

    json_bytes = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed_bytes = zstd.ZstdCompressor(level=3).compress(json_bytes)

    # 告诉上游当前请求体是 zstd 压缩后的 JSON。
    request_headers["content-encoding"] = "zstd"
    if not _header_value_case_insensitive(request_headers, "content-type"):
        request_headers["content-type"] = "application/json"

    return request_headers, {"content": compressed_bytes}


def build_codex_oauth_style_headers(
    *,
    auth_headers: Mapping[str, Any],
    client_headers: Optional[Mapping[str, Any]],
    session_id: Optional[str],
) -> Dict[str, str]:
    """
    Build codex_oauth upstream headers in codex-cli style.
    - Keep auth/account identity from oauth account pool.
    - Keep session_id extraction strategy in handlers (do not synthesize here).
    - Forward x-codex-turn-metadata only when client actually provides it.
    """
    authorization = _header_value_case_insensitive(auth_headers, "authorization")
    account_id = _header_value_case_insensitive(auth_headers, "chatgpt-account-id")

    incoming_originator = _header_value_case_insensitive(client_headers, "originator")
    incoming_user_agent = _header_value_case_insensitive(client_headers, "user-agent")
    incoming_beta_features = _header_value_case_insensitive(client_headers, "x-codex-beta-features")
    incoming_version = _header_value_case_insensitive(client_headers, "version")
    codex_version = incoming_version or _get_codex_upstream_client_version()
    is_codex_downstream = bool(
        incoming_originator.lower() == "codex_cli_rs"
        or "codex_cli_rs" in incoming_user_agent.lower()
        or incoming_beta_features
    )

    out: Dict[str, str] = {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "originator": incoming_originator or CODEX_DEFAULT_ORIGINATOR,
        "user-agent": incoming_user_agent or f"codex_cli_rs/{codex_version} (LLM_PROXY)",
        "x-codex-beta-features": incoming_beta_features or CODEX_DEFAULT_BETA_FEATURES,
        "version": codex_version,
    }

    if authorization:
        out["authorization"] = authorization
    if account_id:
        out["chatgpt-account-id"] = account_id
    if isinstance(session_id, str) and session_id.strip():
        out["session_id"] = session_id.strip()

    # Only forward codex turn metadata when downstream is codex-style.
    turn_metadata = _header_value_case_insensitive(client_headers, "x-codex-turn-metadata")
    if turn_metadata and is_codex_downstream:
        out["x-codex-turn-metadata"] = turn_metadata

    return out


async def build_headers_by_profile(profile: Dict[str, Any], model: str) -> Dict[str, str]:
    auth_type = get_effective_auth_type(profile)
    if auth_type == "codex_oauth":
        return await get_codex_upstream_headers(profile)
    if auth_type == "internal_hw":
        token = await get_x_auth_token()
        return build_auth_headers(profile, model, x_auth_token=token)
    return build_auth_headers(profile, model)


async def mark_retryable_response_for_profile(
    *,
    profile: Dict[str, Any],
    headers: Dict[str, str],
    status_code: int,
    error_text: str = "",
) -> None:
    auth_type = get_effective_auth_type(profile)
    if auth_type != "codex_oauth":
        return
    if status_code in CODEX_AUTH_STATUS_CODES and not should_mark_codex_cooldown(
        status_code, error_text
    ):
        await mark_codex_account_auth_expired(
            headers=headers,
            error_text=error_text,
        )
        return
    if not should_mark_codex_cooldown(status_code, error_text):
        return
    normalized_status = status_code if is_rate_limit_status(status_code) else 429
    await mark_codex_account_rate_limited(
        headers=headers,
        status_code=normalized_status,
        error_text=error_text,
        profile=profile,
    )


async def collect_codex_response_from_stream(
    client: httpx.AsyncClient,
    upstream_url: str,
    profile: Dict[str, Any],
    headers: Dict[str, str],
    request_body: Dict[str, Any],
) -> Dict[str, Any]:
    req_body = dict(request_body)
    req_body["stream"] = True
    request_headers, request_kwargs = build_upstream_request_kwargs(
        profile=profile,
        headers=headers,
        request_body=req_body,
    )
    collected_chunks: List[Any] = []
    try:
        async with client.stream("POST", upstream_url, headers=request_headers, **request_kwargs) as r:
            collected_chunks.append(
                {
                    "type": "codex_stream_meta",
                    "status_code": r.status_code,
                    "headers": dict(r.headers),
                }
            )
            if r.status_code >= 400:
                err = await r.aread()
                err_text = err.decode("utf-8", errors="replace")
                collected_chunks.append({"type": "error_body", "body": err_text})
                return {
                    "ok": False,
                    "status_code": r.status_code,
                    "error_bytes": err,
                    "error_text": err_text,
                    "chunks": collected_chunks,
                }

            text_parts: List[str] = []
            output_items: List[Dict[str, Any]] = []
            completed_response: Dict[str, Any] | None = None
            failed_response: Dict[str, Any] | None = None
            stream_error_text = ""
            async for line in r.aiter_lines():
                if not line:
                    continue
                collected_chunks.append(line)
                if not line.startswith("data:"):
                    continue
                data_part = line[5:].strip()
                if data_part == "[DONE]":
                    break
                try:
                    evt = json.loads(data_part)
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue
                evt_type = str(evt.get("type") or "")
                if evt_type == "response.output_text.delta":
                    delta_text = evt.get("delta")
                    if isinstance(delta_text, str) and delta_text:
                        text_parts.append(delta_text)
                elif evt_type == "response.output_item.done":
                    item = evt.get("item")
                    if isinstance(item, dict):
                        output_items.append(item)
                elif evt_type == "error":
                    error_obj = evt.get("error")
                    if isinstance(error_obj, dict):
                        try:
                            stream_error_text = json.dumps({"error": error_obj}, ensure_ascii=False)
                        except Exception:
                            stream_error_text = str(error_obj)
                    elif not stream_error_text:
                        stream_error_text = data_part
                elif evt_type == "response.failed":
                    response_obj = evt.get("response")
                    if isinstance(response_obj, dict):
                        failed_response = response_obj
                        if not stream_error_text:
                            error_obj = response_obj.get("error")
                            if isinstance(error_obj, dict):
                                try:
                                    stream_error_text = json.dumps({"error": error_obj}, ensure_ascii=False)
                                except Exception:
                                    stream_error_text = str(error_obj)
                            else:
                                try:
                                    stream_error_text = json.dumps({"response": response_obj}, ensure_ascii=False)
                                except Exception:
                                    stream_error_text = data_part
                elif evt_type == "response.completed":
                    response_obj = evt.get("response")
                    if isinstance(response_obj, dict):
                        completed_response = response_obj

            if failed_response is not None or stream_error_text:
                if not stream_error_text and failed_response is not None:
                    try:
                        stream_error_text = json.dumps({"response": failed_response}, ensure_ascii=False)
                    except Exception:
                        stream_error_text = "codex stream failed"
                err_bytes = stream_error_text.encode("utf-8", errors="replace")
                return {
                    "ok": False,
                    "status_code": 502,
                    "error_bytes": err_bytes,
                    "error_text": stream_error_text,
                    "response_json": failed_response,
                    "chunks": collected_chunks,
                }

            if completed_response is None:
                completed_response = {
                    "id": f"resp_{uuid.uuid4().hex}",
                    "object": "response",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]}],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            else:
                # Some models (e.g. gpt-5.5) send `response.completed` with an empty
                # `output` and only deliver the real content (message text + reasoning
                # encrypted_content) via `response.output_item.done` events. Backfill the
                # output from the collected items so downstream text extraction and
                # reasoning re-injection keep working.
                existing_output = completed_response.get("output")
                if not (isinstance(existing_output, list) and existing_output):
                    if output_items:
                        completed_response["output"] = output_items
                    elif text_parts:
                        completed_response["output"] = [
                            {"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]}
                        ]
            return {
                "ok": True,
                "status_code": r.status_code,
                "response_json": completed_response,
                "chunks": collected_chunks,
            }
    except httpx.HTTPError as e:
        err_type = type(e).__name__
        err_msg = str(e).strip() or err_type
        error_payload = {
            "error": {
                "type": "upstream_connection_error",
                "message": f"{err_type}: {err_msg}",
            }
        }
        err_bytes = json.dumps(error_payload, ensure_ascii=False).encode("utf-8")
        collected_chunks.append({"type": "transport_error", "error_type": err_type, "message": err_msg})
        return {
            "ok": False,
            "status_code": 502,
            "error_bytes": err_bytes,
            "error_text": f"{err_type}: {err_msg}",
            "chunks": collected_chunks,
        }

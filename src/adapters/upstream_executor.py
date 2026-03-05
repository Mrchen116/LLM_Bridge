from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import httpx

from token_auth import (
    get_codex_upstream_headers,
    get_x_auth_token,
    mark_codex_account_rate_limited,
)
from upstream_config import build_auth_headers, get_effective_auth_type

RATE_LIMIT_STATUS_CODES = {406, 429}
CODEX_FAILOVER_ERROR_CODES = {
    "insufficient_quota",
    "usage_not_included",
}


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
            if isinstance(err.get("type"), str):
                return str(err.get("type") or "").strip().lower()
        if isinstance(err, str):
            return err.strip().lower()

    lowered = text.lower()
    for code in CODEX_FAILOVER_ERROR_CODES:
        if code in lowered:
            return code
    return ""


def should_trigger_codex_failover(status_code: int, error_text: str = "") -> bool:
    # TODO: 后续根据线上真实 error body 精细化触发条件。
    if is_rate_limit_status(status_code):
        return True
    code = _extract_error_code_from_text(error_text)
    return code in CODEX_FAILOVER_ERROR_CODES


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
    if not should_trigger_codex_failover(status_code, error_text):
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
    headers: Dict[str, str],
    request_body: Dict[str, Any],
) -> Dict[str, Any]:
    req_body = dict(request_body)
    req_body["stream"] = True
    collected_chunks: List[Any] = []
    try:
        async with client.stream("POST", upstream_url, headers=headers, json=req_body) as r:
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
            completed_response: Dict[str, Any] | None = None
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
                elif evt_type == "response.completed":
                    response_obj = evt.get("response")
                    if isinstance(response_obj, dict):
                        completed_response = response_obj

            if completed_response is None:
                completed_response = {
                    "id": f"resp_{uuid.uuid4().hex}",
                    "object": "response",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "".join(text_parts)}]}],
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
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

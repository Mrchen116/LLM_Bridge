from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from upstream_config import (
    PROTOCOL_OPENAI_RESPONSES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_effective_auth_type,
    get_runtime_options,
    resolve_profile,
)


async def handle_openai_responses(req: Request):
    import app as app_module

    body = await req.json()
    body_model = body.get("model")
    model_from_body, _ = app_module._extract_model_and_ban_explore(body_model, app_module.BAN_EXPLORE)
    if model_from_body is not None:
        body["model"] = model_from_body
    stream = bool(body.get("stream", False))

    try:
        resolved = resolve_profile(app_module.UPSTREAM_CONFIG, body, PROTOCOL_OPENAI_RESPONSES)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)
    session_id = req.headers.get("X-Session-Id") or app_module._extract_session_id_from_body_metadata(body)
    codex_reinject_trace: Optional[Dict[str, Any]] = None

    os.makedirs(app_module.LOGS_OPENAI_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
    req_path = os.path.join(app_module.LOGS_OPENAI_DIR, f"{ts}-responses-req.json")
    res_path = os.path.join(app_module.LOGS_OPENAI_DIR, f"{ts}-responses-res.json")

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_RESPONSES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await app_module._build_headers_by_profile(profile, model)
    body["model"] = model
    if auth_type == "codex_oauth":
        body["store"] = False
        body, codex_reinject_trace = app_module._maybe_reinject_codex_reasoning_for_responses(
            session_id=session_id,
            provider=str(profile.get("provider") or ""),
            model=model,
            payload=body,
        )

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    app_module._dump_json(req_path, log_body)

    if not stream:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            r = None
            last_retry_response = None
            for attempt in range(max_retries):
                r = await client.post(upstream_url, headers=upstream_headers, json=body)
                if not app_module.is_rate_limit_status(r.status_code):
                    break
                last_retry_response = r
                logging.warning(f"{attempt} retryable response (responses non-stream): {r.status_code} {r.text}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await app_module._build_headers_by_profile(profile, model)
            if app_module.is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        app_module._dump_json(res_path, app_module._resp_to_obj(r))
        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                app_module._update_codex_reasoning_reinject_cache_for_responses(codex_reinject_trace, r.json())
            except Exception:
                pass
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def sse_passthrough() -> AsyncIterator[bytes]:
        chunks: List[Any] = []
        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                retry_headers = upstream_headers
                last_retry_err_text = None
                last_retry_status = None
                connected = False
                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                        chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        if app_module.is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            chunks.append({"type": "error_body", "body": last_retry_err_text})
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await app_module._build_headers_by_profile(profile, model)
                            continue

                        connected = True
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            chunks.append({"type": "error_body", "body": err_text})
                            yield err
                            return

                        async for raw in r.aiter_raw():
                            chunks.append(raw.decode("utf-8", errors="replace"))
                            yield raw
                        break

                if not connected and last_retry_status is not None and app_module.is_rate_limit_status(last_retry_status):
                    if last_retry_err_text is not None:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    return
        finally:
            app_module._dump_json(res_path, {"type": "responses_passthrough_sse_capture", "chunks": chunks})
            if auth_type == "codex_oauth":
                resp_obj = app_module._extract_response_completed_object_from_sse_chunks(chunks)
                if isinstance(resp_obj, dict):
                    app_module._update_codex_reasoning_reinject_cache_for_responses(codex_reinject_trace, resp_obj)

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")

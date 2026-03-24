from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.adapters.codex_oauth_adapter import collect_with_retry
from src.adapters.http_retry import post_with_retry
from src.adapters.upstream_executor import (
    build_codex_oauth_style_headers,
    build_headers_by_profile,
    build_upstream_request_kwargs,
    collect_codex_response_from_stream,
    is_rate_limit_status,
    mark_retryable_response_for_profile,
    should_retry_codex_result,
    should_trigger_codex_failover,
)
from src.observability.turn_logging import (
    DOWNSTREAM_FORMAT_OPENAI_RESPONSES,
    build_openai_responses_non_stream_from_sse_chunks,
    build_turn_log_paths,
    log_request_phase,
    log_response_phase,
    resolve_raw_bucket,
)
from src.reasoning.reinject import (
    _extract_response_completed_object_from_sse_chunks,
    _extract_session_id_from_headers,
    _extract_session_id_from_body_metadata,
    _maybe_reinject_codex_reasoning_for_responses,
    _update_codex_reasoning_reinject_cache_for_responses,
)
from token_auth import CodexAccountUnavailableError
from proxy_converters import (
    _extract_model_and_ban_explore,
    ensure_codex_responses_include_encrypted_reasoning,
    resolve_codex_upstream_reasoning_effort,
)
from proxy_logging import _resp_to_obj
from upstream_config import (
    PROTOCOL_OPENAI_RESPONSES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_codex_oauth_max_failovers,
    get_codex_oauth_retry_attempts,
    get_effective_auth_type,
    get_runtime_options,
    resolve_profile,
)


async def _prepend_stream_chunk(first_chunk: bytes, iterator: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first_chunk
    async for chunk in iterator:
        yield chunk


async def run_responses_flow(
    req: Request,
    *,
    ban_explore: bool,
    upstream_config: Dict[str, Any],
    logs_raw_dir: str,
    logs_session_dir: str,
):
    body = await req.json()
    body_model = body.get("model")
    model_from_body, _ = _extract_model_and_ban_explore(body_model, ban_explore)
    if model_from_body is not None:
        body["model"] = model_from_body
    stream = bool(body.get("stream", False))

    try:
        resolved = resolve_profile(upstream_config, body, PROTOCOL_OPENAI_RESPONSES)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)
    session_id = _extract_session_id_from_headers(req.headers) or _extract_session_id_from_body_metadata(body)
    codex_reinject_trace: Optional[Dict[str, Any]] = None

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_RESPONSES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    max_failovers = 0
    if auth_type == "codex_oauth":
        max_retries = get_codex_oauth_retry_attempts(profile, max_retries)
        max_failovers = get_codex_oauth_max_failovers(profile)

    async def refresh_upstream_headers() -> Dict[str, str]:
        headers = await build_headers_by_profile(profile, model)
        if auth_type != "codex_oauth":
            return headers
        return build_codex_oauth_style_headers(
            auth_headers=headers,
            client_headers=req.headers,
            session_id=session_id,
        )

    try:
        upstream_headers = await refresh_upstream_headers()
    except CodexAccountUnavailableError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": e.error_type}},
            status_code=e.status_code,
        )
    body["model"] = model
    if auth_type == "codex_oauth":
        reasoning = body.get("reasoning")
        reasoning_dict: Dict[str, Any] = dict(reasoning) if isinstance(reasoning, dict) else {}
        reasoning_dict["effort"] = resolve_codex_upstream_reasoning_effort(
            reasoning_effort_top=body.get("reasoning_effort"),
            reasoning_dict=reasoning_dict if reasoning_dict else None,
            model_suffix_effort=resolved.reasoning_effort,
        )
        body["reasoning"] = reasoning_dict
        body.pop("reasoning_effort", None)
        body["store"] = False
        body, codex_reinject_trace = _maybe_reinject_codex_reasoning_for_responses(
            session_id=session_id,
            provider=str(profile.get("provider") or ""),
            model=model,
            payload=body,
        )
        # 与 chat/completions 转 Codex 路径一致：无状态多轮需要上游返回 reasoning.encrypted_content。
        ensure_codex_responses_include_encrypted_reasoning(body)

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    upstream_request_body = dict(body)
    if auth_type == "codex_oauth" and not stream:
        # codex upstream currently requires stream=true; we collect internally and return JSON downstream.
        upstream_request_body["stream"] = True

    turn_logs = build_turn_log_paths(
        logs_raw_dir=logs_raw_dir,
        logs_session_dir=logs_session_dir,
        raw_bucket=resolve_raw_bucket(auth_type=auth_type, provider=str(profile.get("provider") or "")),
        downstream_format=DOWNSTREAM_FORMAT_OPENAI_RESPONSES,
        session_id=session_id,
    )
    log_request_phase(
        turn_logs,
        request_obj=log_body,
        upstream_request_obj=upstream_request_body,
        client_headers=req.headers,
        upstream_headers=upstream_headers,
    )

    if not stream:
        if auth_type == "codex_oauth":
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                result = await collect_with_retry(
                    collect_once=lambda hdrs: collect_codex_response_from_stream(
                        client=client,
                        upstream_url=upstream_url,
                        profile=profile,
                        headers=hdrs,
                        request_body=upstream_request_body,
                    ),
                    headers=upstream_headers,
                    max_retries=max_retries,
                    max_failovers=max_failovers,
                    is_retryable=is_rate_limit_status,
                    refresh_headers=refresh_upstream_headers,
                    on_retryable_response=lambda hdrs, status, err: mark_retryable_response_for_profile(
                        profile=profile,
                        headers=hdrs,
                        status_code=status,
                        error_text=err,
                    ),
                    should_retry_result=lambda result: should_retry_codex_result(
                        int(result.get("status_code") or 0),
                        str(result.get("error_text") or ""),
                    ),
                    should_failover_result=lambda result: should_trigger_codex_failover(
                        int(result.get("status_code") or 0),
                        str(result.get("error_text") or ""),
                    ),
                )

            upstream_obj = {"type": "codex_nonstream_bridge_capture", "chunks": result.get("chunks") or []}
            if not bool(result.get("ok")):
                downstream_obj = {
                    "status_code": int(result.get("status_code") or 500),
                    "media_type": "application/json",
                    "body": str(result.get("error_text") or ""),
                }
                log_response_phase(
                    turn_logs,
                    upstream_response_obj=upstream_obj,
                    downstream_response_obj=downstream_obj,
                    non_stream_response_obj=downstream_obj,
                )
                return Response(
                    content=result.get("error_bytes") or b"",
                    status_code=int(result.get("status_code") or 500),
                    media_type="application/json",
                )

            response_obj = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
            _update_codex_reasoning_reinject_cache_for_responses(codex_reinject_trace, response_obj)
            downstream_obj = {"status_code": 200, "json": response_obj}
            log_response_phase(
                turn_logs,
                upstream_response_obj=upstream_obj,
                downstream_response_obj=downstream_obj,
                non_stream_response_obj=response_obj,
            )
            return JSONResponse(content=response_obj, status_code=200)

        r = await post_with_retry(
            profile=profile,
            upstream_url=upstream_url,
            request_body=upstream_request_body,
            headers=upstream_headers,
            max_retries=max_retries,
            is_retryable=is_rate_limit_status,
            refresh_headers=refresh_upstream_headers,
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
        )

        upstream_obj = _resp_to_obj(r)
        downstream_obj: Dict[str, Any] = {
            "status_code": r.status_code,
            "headers": dict(r.headers),
        }
        try:
            downstream_obj["json"] = r.json()
            non_stream_obj = downstream_obj["json"] if isinstance(downstream_obj["json"], dict) else downstream_obj
        except Exception:
            downstream_obj["text"] = r.text
            non_stream_obj = downstream_obj
        log_response_phase(
            turn_logs,
            upstream_response_obj=upstream_obj,
            downstream_response_obj=downstream_obj,
            non_stream_response_obj=non_stream_obj,
        )
        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                _update_codex_reasoning_reinject_cache_for_responses(codex_reinject_trace, r.json())
            except Exception:
                pass
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    stream_result: Dict[str, Any] = {}

    async def sse_passthrough() -> AsyncIterator[bytes]:
        chunks: List[Any] = []
        down_chunks: List[Any] = []
        downstream_obj: Dict[str, Any] = {"type": "responses_sse_capture", "chunks": down_chunks}
        non_stream_obj: Optional[Dict[str, Any]] = None

        def emit_bytes(raw: bytes) -> bytes:
            down_chunks.append(raw.decode("utf-8", errors="replace"))
            return raw

        def set_error_response(*, status_code: int, body: str, media_type: str = "application/json") -> None:
            nonlocal downstream_obj, non_stream_obj
            error_obj = {
                "type": "passthrough_error",
                "status_code": status_code,
                "media_type": media_type,
                "body": body,
            }
            downstream_obj = error_obj
            non_stream_obj = error_obj
            stream_result["error_response"] = error_obj

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
                    request_headers, request_kwargs = build_upstream_request_kwargs(
                        profile=profile,
                        headers=retry_headers,
                        request_body=body,
                    )
                    async with client.stream("POST", upstream_url, headers=request_headers, **request_kwargs) as r:
                        chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        if r.status_code >= 400:
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            chunks.append({"type": "error_body", "body": last_retry_err_text})
                            retryable = (
                                should_trigger_codex_failover(r.status_code, last_retry_err_text)
                                if auth_type == "codex_oauth"
                                else is_rate_limit_status(r.status_code)
                            )
                            if retryable:
                                await mark_retryable_response_for_profile(
                                    profile=profile,
                                    headers=retry_headers,
                                    status_code=r.status_code,
                                    error_text=last_retry_err_text,
                                )
                                if attempt < max_retries - 1:
                                    await asyncio.sleep(1 * (2 ** attempt))
                                    retry_headers = await refresh_upstream_headers()
                                    continue
                            set_error_response(
                                status_code=r.status_code,
                                body=last_retry_err_text or json.dumps(
                                    {
                                        "error": {
                                            "message": "上游返回异常状态，但响应体为空",
                                            "type": "upstream_http_error",
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            return

                        connected = True

                        async for raw in r.aiter_raw():
                            chunks.append(raw.decode("utf-8", errors="replace"))
                            yield emit_bytes(raw)
                        if not down_chunks:
                            set_error_response(
                                status_code=502,
                                body=json.dumps(
                                    {
                                        "error": {
                                            "message": "上游建立了流式连接，但未返回任何数据块",
                                            "type": "upstream_empty_stream",
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            return
                        break

                if not connected and last_retry_status is not None:
                    should_emit_last = (
                        should_trigger_codex_failover(last_retry_status, last_retry_err_text or "")
                        if auth_type == "codex_oauth"
                        else is_rate_limit_status(last_retry_status)
                    )
                    if not should_emit_last:
                        set_error_response(
                            status_code=last_retry_status,
                            body=last_retry_err_text or json.dumps(
                                {
                                    "error": {
                                        "message": "上游返回异常状态，但响应体为空",
                                        "type": "upstream_http_error",
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        )
                        return
                    set_error_response(
                        status_code=last_retry_status,
                        body=last_retry_err_text or json.dumps(
                            {
                                "error": {
                                    "message": "上游返回异常状态，但响应体为空",
                                    "type": "upstream_http_error",
                                }
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return
                if not connected:
                    set_error_response(
                        status_code=502,
                        body=json.dumps(
                            {
                                "error": {
                                    "message": "上游流式请求未建立连接",
                                    "type": "upstream_no_response",
                                }
                            },
                            ensure_ascii=False,
                        ),
                    )
                    return
        except httpx.HTTPError as e:
            set_error_response(
                status_code=502,
                body=json.dumps(
                    {
                        "error": {
                            "message": f"{type(e).__name__}: {e}",
                            "type": "upstream_connection_error",
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        finally:
            upstream_obj = {"type": "responses_passthrough_sse_capture", "chunks": chunks}
            if non_stream_obj is None:
                non_stream_obj = build_openai_responses_non_stream_from_sse_chunks(down_chunks)
            log_response_phase(
                turn_logs,
                upstream_response_obj=upstream_obj,
                downstream_response_obj=downstream_obj,
                non_stream_response_obj=non_stream_obj,
            )
            if auth_type == "codex_oauth":
                resp_obj = _extract_response_completed_object_from_sse_chunks(chunks)
                if isinstance(resp_obj, dict):
                    _update_codex_reasoning_reinject_cache_for_responses(codex_reinject_trace, resp_obj)

    iterator = sse_passthrough()
    try:
        first_chunk = await anext(iterator)
    except StopAsyncIteration:
        error_obj = stream_result.get("error_response") or {
            "status_code": 502,
            "media_type": "application/json",
            "body": json.dumps(
                {
                    "error": {
                        "message": "上游流式请求未返回任何内容",
                        "type": "upstream_empty_stream",
                    }
                },
                ensure_ascii=False,
            ),
        }
        return Response(
            content=str(error_obj.get("body") or ""),
            status_code=int(error_obj.get("status_code") or 502),
            media_type=str(error_obj.get("media_type") or "application/json"),
        )
    return StreamingResponse(_prepend_stream_chunk(first_chunk, iterator), media_type="text/event-stream")

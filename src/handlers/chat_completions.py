from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
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
from src.bridge.openai_codex import (
    codex_response_to_openai_chat_completion,
    openai_chat_body_to_codex_payload,
)
from src.observability.turn_logging import (
    DOWNSTREAM_FORMAT_OPENAI_CHAT,
    build_openai_chat_non_stream_from_sse_chunks,
    build_turn_log_paths,
    log_request_phase,
    log_response_phase,
    resolve_raw_bucket,
)
from src.reasoning.reinject import (
    _extract_session_id_from_headers,
    _extract_session_id_from_body_metadata,
    _maybe_reinject_codex_reasoning,
    _update_codex_reasoning_reinject_cache,
)
from token_auth import CodexAccountUnavailableError
from proxy_converters import (
    _extract_model_and_ban_explore,
    _strip_task_explore_line,
)
from proxy_logging import _resp_to_obj
from upstream_config import (
    PROTOCOL_OPENAI_CHAT,
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


async def run_chat_completions_flow(
    req: Request,
    *,
    ban_explore: bool,
    upstream_config: Dict[str, Any],
    logs_raw_dir: str,
    logs_session_dir: str,
):
    body = await req.json()
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, ban_explore)
    if model_from_body is not None:
        body["model"] = model_from_body

    try:
        resolved = resolve_profile(upstream_config, body, PROTOCOL_OPENAI_CHAT)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)

    session_id = _extract_session_id_from_headers(req.headers) or _extract_session_id_from_body_metadata(body)

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_CHAT)
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

    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    upstream_request_body = body
    codex_reinject_trace: Optional[Dict[str, Any]] = None
    if auth_type == "codex_oauth":
        codex_chat_body = dict(body)
        upstream_request_body = openai_chat_body_to_codex_payload(
            codex_chat_body, model, model_suffix_effort=resolved.reasoning_effort
        )
        upstream_request_body, codex_reinject_trace = _maybe_reinject_codex_reasoning(
            session_id=session_id,
            provider=str(profile.get("provider") or ""),
            model=model,
            codex_chat_body=codex_chat_body,
            codex_payload=upstream_request_body,
            model_suffix_effort=resolved.reasoning_effort,
        )

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    if auth_type == "codex_oauth":
        log_body["_upstream_payload_kind"] = "codex_responses"

    turn_logs = build_turn_log_paths(
        logs_raw_dir=logs_raw_dir,
        logs_session_dir=logs_session_dir,
        raw_bucket=resolve_raw_bucket(auth_type=auth_type, provider=str(profile.get("provider") or "")),
        downstream_format=DOWNSTREAM_FORMAT_OPENAI_CHAT,
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

            try:
                codex_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_json)
                converted = codex_response_to_openai_chat_completion(codex_json, model)
                downstream_obj = {"status_code": 200, "json": converted}
                log_response_phase(
                    turn_logs,
                    upstream_response_obj=upstream_obj,
                    downstream_response_obj=downstream_obj,
                    non_stream_response_obj=converted,
                )
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                fallback_obj = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                downstream_obj = {"status_code": 200, "json": fallback_obj}
                log_response_phase(
                    turn_logs,
                    upstream_response_obj=upstream_obj,
                    downstream_response_obj=downstream_obj,
                    non_stream_response_obj=fallback_obj,
                )
                return JSONResponse(content=fallback_obj, status_code=200)

        r = await post_with_retry(
            profile=profile,
            upstream_url=upstream_url,
            request_body=upstream_request_body if auth_type == "codex_oauth" else body,
            headers=upstream_headers,
            max_retries=max_retries,
            is_retryable=is_rate_limit_status,
            refresh_headers=refresh_upstream_headers,
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
        )

        upstream_obj = _resp_to_obj(r)

        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                codex_json = r.json()
                converted = codex_response_to_openai_chat_completion(codex_json, model)
                downstream_obj = {"status_code": r.status_code, "headers": dict(r.headers), "json": converted}
                log_response_phase(
                    turn_logs,
                    upstream_response_obj=upstream_obj,
                    downstream_response_obj=downstream_obj,
                    non_stream_response_obj=converted,
                )
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                pass

        downstream_obj: Dict[str, Any] = {
            "status_code": r.status_code,
            "headers": dict(r.headers),
        }
        try:
            downstream_obj["json"] = r.json()
            non_stream_obj: Dict[str, Any] = downstream_obj["json"] if isinstance(downstream_obj["json"], dict) else downstream_obj
        except Exception:
            downstream_obj["text"] = r.text
            non_stream_obj = downstream_obj
        log_response_phase(
            turn_logs,
            upstream_response_obj=upstream_obj,
            downstream_response_obj=downstream_obj,
            non_stream_response_obj=non_stream_obj,
        )

        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    stream_result: Dict[str, Any] = {}

    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        down_chunks: List[Any] = []
        downstream_obj: Dict[str, Any] = {"type": "openai_chat_sse_capture", "chunks": down_chunks}
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
                    up_chunks.extend(result.get("chunks") or [])
                    if not bool(result.get("ok")):
                        err_text = str(result.get("error_text") or "")
                        set_error_response(
                            status_code=int(result.get("status_code") or 500),
                            body=err_text or json.dumps(
                                {
                                    "error": {
                                        "message": "上游返回异常状态，但响应体为空",
                                        "type": "upstream_http_error",
                                        "code": int(result.get("status_code") or 500),
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        )
                        return

                    codex_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                    _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_json)
                    converted = codex_response_to_openai_chat_completion(codex_json, model)
                    message_obj = (
                        converted.get("choices", [{}])[0].get("message", {})
                        if isinstance(converted.get("choices"), list)
                        else {}
                    )
                    content_text = (
                        message_obj.get("content", "")
                        if isinstance(message_obj, dict)
                        else ""
                    )
                    tool_calls = (
                        message_obj.get("tool_calls", [])
                        if isinstance(message_obj, dict) and isinstance(message_obj.get("tool_calls"), list)
                        else []
                    )
                    chunk_id = str(converted.get("id") or f"chatcmpl-{uuid.uuid4().hex}")
                    created_ts = int(time.time())
                    if content_text:
                        first_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": content_text}, "finish_reason": None}],
                        }
                        yield emit_bytes(f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))

                    if not content_text and not tool_calls:
                        set_error_response(
                            status_code=502,
                            body=json.dumps(
                                {
                                    "error": {
                                        "message": "上游返回成功，但未生成任何文本或工具调用",
                                        "type": "upstream_empty_stream",
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        )
                        return

                    if tool_calls:
                        normalized_tool_calls: List[Dict[str, Any]] = []
                        for idx, tc in enumerate(tool_calls):
                            if isinstance(tc, dict):
                                normalized_tc = dict(tc)
                                normalized_tc.setdefault("index", idx)
                                normalized_tool_calls.append(normalized_tc)
                            else:
                                normalized_tool_calls.append({"index": idx})

                        tool_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [{"index": 0, "delta": {"tool_calls": normalized_tool_calls}, "finish_reason": None}],
                        }
                        yield emit_bytes(f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))

                    last_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tool_calls else "stop"}],
                        "usage": converted.get("usage"),
                    }
                    yield emit_bytes(f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    yield emit_bytes(b"data: [DONE]\n\n")
                    return

            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                last_retry_err_text = None
                last_retry_status = None
                retry_headers = upstream_headers
                connection_established = False

                has_valid_content = False
                content_buffer = ""

                for attempt in range(max_retries):
                    request_headers, request_kwargs = build_upstream_request_kwargs(
                        profile=profile,
                        headers=retry_headers,
                        request_body=body,
                    )
                    async with client.stream("POST", upstream_url, headers=request_headers, **request_kwargs) as r:
                        meta = {
                            "type": "openai_passthrough_sse_meta",
                            "status_code": r.status_code,
                            "headers": dict(r.headers),
                        }
                        up_chunks.append(meta)

                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                            await mark_retryable_response_for_profile(
                                profile=profile,
                                headers=retry_headers,
                                status_code=r.status_code,
                                error_text=last_retry_err_text,
                            )
                            logging.warning(
                                f"{attempt} retryable response (chat/completions stream): {r.status_code} {last_retry_err_text}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await refresh_upstream_headers()
                            continue

                        connection_established = True

                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            up_chunks.append({"type": "error_body", "body": err_text})
                            set_error_response(
                                status_code=r.status_code,
                                body=err_text or json.dumps(
                                    {
                                        "error": {
                                            "message": "上游返回异常状态，但响应体为空",
                                            "type": "upstream_http_error",
                                            "code": r.status_code,
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            return

                        async for line in r.aiter_lines():
                            if not line:
                                continue

                            up_chunks.append(line)

                            if line.startswith("data:"):
                                data_part = line[5:].strip()

                                if data_part == "[DONE]":
                                    if has_valid_content:
                                        yield emit_bytes(b"data: [DONE]\n\n")
                                    break

                                try:
                                    chunk_data = json.loads(data_part)

                                    choices = chunk_data.get("choices", [])
                                    finish_reason = None
                                    if choices and len(choices) > 0:
                                        choice = choices[0]
                                        delta = choice.get("delta", {})
                                        content = delta.get("content")
                                        reasoning_content = delta.get("reasoning_content")
                                        reasoning = delta.get("reasoning")
                                        tool_calls = delta.get("tool_calls")
                                        finish_reason = choice.get("finish_reason")

                                        if content is not None and content != "":
                                            has_valid_content = True
                                            content_buffer += content
                                        elif content is not None and content == "" and len(content_buffer) > 0:
                                            has_valid_content = True
                                        if reasoning_content is not None and reasoning_content != "":
                                            has_valid_content = True
                                            content_buffer += reasoning_content
                                        if reasoning is not None and reasoning != "":
                                            has_valid_content = True
                                            content_buffer += reasoning
                                        if tool_calls is not None and len(tool_calls) > 0:
                                            has_valid_content = True

                                        if finish_reason is not None:
                                            has_valid_content = True

                                    if "usage" in chunk_data:
                                        choices = chunk_data.get("choices", [])
                                        has_finish_reason = False
                                        if choices and len(choices) > 0:
                                            finish_reason = choices[0].get("finish_reason")
                                            if finish_reason is not None:
                                                has_finish_reason = True

                                        if not has_finish_reason:
                                            chunk_data.pop("usage", None)

                                    should_emit_tool_calls = False
                                    if finish_reason == "tool_calls" and choices:
                                        tool_calls_flat = []
                                        for choice in choices:
                                            if isinstance(choice, dict):
                                                delta = choice.get("delta", {})
                                                tcs = delta.get("tool_calls", [])
                                                if isinstance(tcs, list):
                                                    tool_calls_flat.extend(tcs)

                                        if tool_calls_flat or has_valid_content:
                                            should_emit_tool_calls = True

                                    yield emit_bytes(f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n".encode("utf-8"))

                                    if should_emit_tool_calls:
                                        if has_valid_content:
                                            final_chunk = {
                                                "id": "chatcmpl-final",
                                                "object": "chat.completion.chunk",
                                                "created": chunk_data.get("created", int(time.time())),
                                                "model": chunk_data.get("model", body.get("model", "unknown")),
                                                "choices": [{
                                                    "index": 0,
                                                    "delta": {"content": ""},
                                                    "finish_reason": "tool_calls"
                                                }]
                                            }
                                            yield emit_bytes(
                                                f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                                            )

                                        yield emit_bytes(b"data: [DONE]\n\n")
                                        return
                                except json.JSONDecodeError:
                                    yield emit_bytes(line.encode("utf-8") + b"\n\n")
                            else:
                                yield emit_bytes(line.encode("utf-8") + b"\n")

                    if connection_established:
                        if not has_valid_content:
                            set_error_response(
                                status_code=502,
                                body=json.dumps(
                                    {
                                        "error": {
                                            "message": "上游建立了流式连接，但未返回任何有效内容",
                                            "type": "upstream_empty_stream",
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                            return

                        yield emit_bytes(b"data: [DONE]\n\n")
                        break

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await refresh_upstream_headers()

                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
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
                if not connection_established:
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
            upstream_obj = {"type": "openai_passthrough_sse_capture", "chunks": up_chunks}
            if non_stream_obj is None:
                non_stream_obj = build_openai_chat_non_stream_from_sse_chunks(down_chunks, model)
            log_response_phase(
                turn_logs,
                upstream_response_obj=upstream_obj,
                downstream_response_obj=downstream_obj,
                non_stream_response_obj=non_stream_obj,
            )

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

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from src.runtime.context import RuntimeContext

from upstream_config import (
    PROTOCOL_OPENAI_CHAT,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_effective_auth_type,
    get_runtime_options,
    resolve_profile,
)


async def handle_openai_chat_completions(req: Request, ctx: RuntimeContext):
    _extract_model_and_ban_explore = ctx.converters._extract_model_and_ban_explore
    BAN_EXPLORE = ctx.ban_explore
    UPSTREAM_CONFIG = ctx.upstream_config
    LOGS_CODEAGENT_DIR = ctx.logs_codeagent_dir
    LOGS_OPENAI_DIR = ctx.logs_openai_dir
    _build_headers_by_profile = ctx.executor.build_headers_by_profile
    _strip_task_explore_line = ctx.converters._strip_task_explore_line
    _build_codex_responses_payload_from_chat = ctx.converters._build_codex_responses_payload_from_chat
    _maybe_reinject_codex_reasoning = ctx.reasoning._maybe_reinject_codex_reasoning
    _dump_json = ctx.proxy_logging._dump_json
    _collect_codex_response_from_stream = ctx.executor.collect_codex_response_from_stream
    is_rate_limit_status = ctx.executor.is_rate_limit_status
    _update_codex_reasoning_reinject_cache = ctx.reasoning._update_codex_reasoning_reinject_cache
    _codex_responses_to_chat_completion = ctx.converters._codex_responses_to_chat_completion
    _resp_to_obj = ctx.proxy_logging._resp_to_obj

    body = await req.json()
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, BAN_EXPLORE)
    if model_from_body is not None:
        body["model"] = model_from_body

    try:
        resolved = resolve_profile(UPSTREAM_CONFIG, body, PROTOCOL_OPENAI_CHAT)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)

    session_id = req.headers.get("X-Session-Id")

    if session_id:
        os.makedirs(LOGS_CODEAGENT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
        existing_dirs = sorted(glob.glob(os.path.join(LOGS_CODEAGENT_DIR, f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join(LOGS_CODEAGENT_DIR, f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        req_path = os.path.join(session_dir, f"{ts}-req.json")
        res_path = os.path.join(session_dir, f"{ts}--res.json")
    else:
        os.makedirs(LOGS_OPENAI_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
        req_path = os.path.join(LOGS_OPENAI_DIR, f"{ts}-req.json")
        res_path = os.path.join(LOGS_OPENAI_DIR, f"{ts}--res.json")

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_CHAT)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await _build_headers_by_profile(profile, model)

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
        upstream_request_body = _build_codex_responses_payload_from_chat(codex_chat_body, model)
        upstream_request_body, codex_reinject_trace = _maybe_reinject_codex_reasoning(
            session_id=session_id,
            provider=str(profile.get("provider") or ""),
            model=model,
            codex_chat_body=codex_chat_body,
            codex_payload=upstream_request_body,
        )

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    if auth_type == "codex_oauth":
        log_body["_upstream_payload_kind"] = "codex_responses"
    _dump_json(req_path, log_body)

    if not stream:
        if auth_type == "codex_oauth":
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                result: Dict[str, Any] = {}
                last_retry_result: Optional[Dict[str, Any]] = None
                retry_headers = upstream_headers
                for attempt in range(max_retries):
                    result = await _collect_codex_response_from_stream(
                        client=client,
                        upstream_url=upstream_url,
                        headers=retry_headers,
                        request_body=upstream_request_body,
                    )
                    status_code = int(result.get("status_code") or 0)
                    if not is_rate_limit_status(status_code):
                        break
                    last_retry_result = result
                    err_msg = result.get("error_text") or ""
                    logging.warning(
                        f"{attempt} retryable response (chat/completions codex non-stream): {status_code} {err_msg}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)
                if is_rate_limit_status(int(result.get("status_code") or 0)) and last_retry_result is not None:
                    result = last_retry_result

            _dump_json(res_path, {"type": "codex_nonstream_bridge_capture", "chunks": result.get("chunks") or []})
            if not bool(result.get("ok")):
                return Response(
                    content=result.get("error_bytes") or b"",
                    status_code=int(result.get("status_code") or 500),
                    media_type="application/json",
                )

            try:
                codex_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_json)
                converted = _codex_responses_to_chat_completion(codex_json, model)
                _dump_json(res_path, {"status_code": 200, "json": converted})
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                fallback_obj = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                return JSONResponse(content=fallback_obj, status_code=200)

        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            r = None
            last_retry_response = None

            for attempt in range(max_retries):
                if auth_type == "codex_oauth":
                    r = await client.post(upstream_url, headers=upstream_headers, json=upstream_request_body)
                else:
                    r = await client.post(upstream_url, headers=upstream_headers, json=body)

                if not is_rate_limit_status(r.status_code):
                    break

                last_retry_response = r
                logging.warning(f"{attempt} retryable response (chat/completions non-stream): {r.status_code} {r.text}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await _build_headers_by_profile(profile, model)

            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        _dump_json(res_path, _resp_to_obj(r))

        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                codex_json = r.json()
                converted = _codex_responses_to_chat_completion(codex_json, model)
                _dump_json(res_path, {"status_code": r.status_code, "headers": dict(r.headers), "json": converted})
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                pass

        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        try:
            if auth_type == "codex_oauth":
                async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(timeout_seconds),
                    trust_env=trust_env,
                ) as client:
                    result = await _collect_codex_response_from_stream(
                        client=client,
                        upstream_url=upstream_url,
                        headers=upstream_headers,
                        request_body=upstream_request_body,
                    )
                    up_chunks.extend(result.get("chunks") or [])
                    if not bool(result.get("ok")):
                        err_text = str(result.get("error_text") or "")
                        error_data = {
                            "id": "chatcmpl-error",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                            "error": {
                                "message": err_text,
                                "type": "upstream_error",
                                "code": int(result.get("status_code") or 500),
                            },
                        }
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

                    codex_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                    _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_json)
                    converted = _codex_responses_to_chat_completion(codex_json, model)
                    content_text = (
                        converted.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if isinstance(converted.get("choices"), list)
                        else ""
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
                        yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                    last_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": converted.get("usage"),
                    }
                    yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
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
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
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
                            logging.warning(
                                f"{attempt} retryable response (chat/completions stream): {r.status_code} {last_retry_err_text}")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        connection_established = True

                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            up_chunks.append({"type": "error_body", "body": err_text})
                            error_data = {
                                "id": "chatcmpl-error",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": body.get("model", "unknown"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "error"
                                }]
                            }

                            try:
                                error_json = json.loads(err_text)
                                if isinstance(error_json, dict):
                                    error_data["error"] = error_json
                            except Exception:
                                error_data["error"] = {
                                    "message": err_text,
                                    "type": "upstream_error",
                                    "code": r.status_code
                                }

                            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return

                        async for line in r.aiter_lines():
                            if not line:
                                continue

                            up_chunks.append(line)

                            if line.startswith("data:"):
                                data_part = line[5:].strip()

                                if data_part == "[DONE]":
                                    if has_valid_content:
                                        yield b"data: [DONE]\n\n"
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

                                    yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n".encode("utf-8")

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
                                            yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode(
                                                "utf-8")

                                        yield b"data: [DONE]\n\n"
                                        return
                                except json.JSONDecodeError:
                                    yield line.encode("utf-8") + b"\n\n"
                            else:
                                yield line.encode("utf-8") + b"\n"

                    if connection_established:
                        if not has_valid_content:
                            empty_chunk = {
                                "id": "chatcmpl-empty",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": body.get("model", "unknown"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(empty_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                        yield b"data: [DONE]\n\n"
                        break

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)

                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    if last_retry_err_text is not None:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    yield b"data: [DONE]\n\n"
                    return
        finally:
            _dump_json(res_path, {"type": "openai_passthrough_sse_capture", "chunks": up_chunks})

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")

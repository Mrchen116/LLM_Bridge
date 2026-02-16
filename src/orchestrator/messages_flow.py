from __future__ import annotations

import asyncio
import glob
import json
import os
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from src.adapters.codex_oauth_adapter import collect_with_retry
from src.adapters.http_retry import post_with_retry
from src.adapters.upstream_executor import (
    build_headers_by_profile,
    collect_codex_response_from_stream,
    is_rate_limit_status,
)
from src.orchestrator.reasoning_reinject import (
    _extract_session_id_from_body_metadata,
    _maybe_reinject_codex_reasoning,
    _update_codex_reasoning_reinject_cache,
)
from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _codex_responses_to_chat_completion,
    _extract_codex_output_tool_uses,
    _extract_model_and_ban_explore,
    _strip_task_explore_line,
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai_tools,
    oai_finish_reason_to_stop_reason,
)
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _discard_session_req,
    _dump_json,
    _extract_usage_from_obj,
    _parse_anthropic_sse_chunks_to_events,
    _resp_to_obj,
    _should_skip_session_logging,
    _sse_event,
    _usage_dict_has_tokens,
)

from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_effective_auth_type,
    get_runtime_options,
    resolve_profile,
)


async def _forward_anthropic_native_messages(
    body: Dict[str, Any],
    stream: bool,
    profile: Dict[str, Any],
    model: str,
    req_path: str,
    up_res_path: str,
    down_res_path: str,
    session_req_path: Optional[str],
    session_down_res_path: Optional[str],
    session_non_stream_path: Optional[str],
) -> Response:
    upstream_url = build_upstream_url(profile, PROTOCOL_ANTHROPIC_MESSAGES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    payload = dict(body)
    payload["model"] = model

    if not stream:
        headers = await build_headers_by_profile(profile, model)
        r = await post_with_retry(
            upstream_url=upstream_url,
            request_body=payload,
            headers=headers,
            max_retries=max_retries,
            is_retryable=is_rate_limit_status,
            refresh_headers=lambda: build_headers_by_profile(profile, model),
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
        )

        _dump_json(up_res_path, _resp_to_obj(r))
        down_obj = {
            "type": "anthropic_passthrough_response",
            "status_code": r.status_code,
            "headers": dict(r.headers),
        }
        try:
            down_obj["json"] = r.json()
        except Exception:
            down_obj["text"] = r.text
        _dump_json(down_res_path, down_obj)
        if session_down_res_path:
            usage = _extract_usage_from_obj(down_obj)
            if not _usage_dict_has_tokens(usage):
                _discard_session_req(session_req_path)
            else:
                _dump_json(session_down_res_path, down_obj)

        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        down_chunks: List[Any] = []
        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                last_retry_err_text = None
                last_retry_status = None
                connection_established = False
                retry_headers = await build_headers_by_profile(profile, model)

                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=payload) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await build_headers_by_profile(profile, model)
                            continue

                        connection_established = True
                        async for chunk in r.aiter_raw():
                            down_chunks.append(chunk.decode("utf-8", errors="replace"))
                            yield chunk
                        return

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await build_headers_by_profile(profile, model)

                if (not connection_established) and (last_retry_status is not None):
                    if last_retry_err_text:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    return
        finally:
            _dump_json(up_res_path, {"type": "anthropic_native_sse_capture", "chunks": up_chunks})
            _dump_json(down_res_path, {"type": "anthropic_native_sse_capture", "chunks": down_chunks})
            if session_down_res_path:
                events = _parse_anthropic_sse_chunks_to_events(down_chunks)
                non_stream_resp = _build_anthropic_non_stream_from_events(events, model)
                usage = _extract_usage_from_obj(non_stream_resp) if non_stream_resp else None
                if not _usage_dict_has_tokens(usage):
                    _discard_session_req(session_req_path)
                else:
                    _dump_json(session_down_res_path, {"type": "anthropic_native_sse_capture", "chunks": down_chunks})
                    if session_non_stream_path and non_stream_resp:
                        _dump_json(session_non_stream_path, non_stream_resp)

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")


async def run_messages_flow(
    req: Request,
    *,
    ban_stream: bool,
    ban_explore: bool,
    expose_thinking: bool,
    upstream_config: Dict[str, Any],
    logs_anthropic_dir: str,
    logs_session_dir: str,
):
    body = await req.json()
    body_stream = bool(body.get("stream", False))
    header_stream = req.headers.get("x-stainless-helper-method", "").lower() == "stream"
    stream = body_stream or header_stream
    if stream and ban_stream:
        return JSONResponse(
            {
                "error": {
                    "message": "暂不支持流式请求，请使用非流式模式重试",
                    "type": "stream_disabled",
                }
            },
            status_code=400,
        )
    os.makedirs(logs_anthropic_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]

    session_id = _extract_session_id_from_body_metadata(body)
    skip_session_logging = False
    if session_id:
        skip_session_logging = _should_skip_session_logging(body)

    req_path = os.path.join(logs_anthropic_dir, f"{ts}-req.json")
    up_res_path = os.path.join(logs_anthropic_dir, f"{ts}-upstream-res.json")
    down_res_path = os.path.join(logs_anthropic_dir, f"{ts}-downstream-res.json")
    headers_path = os.path.join(logs_anthropic_dir, f"{ts}-headers.json")

    session_req_path = None
    session_down_res_path = None
    session_non_stream_path = None
    if session_id and not skip_session_logging:
        os.makedirs(logs_session_dir, exist_ok=True)
        existing_dirs = sorted(glob.glob(os.path.join(logs_session_dir, f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join(logs_session_dir, f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        session_req_path = os.path.join(session_dir, f"{ts}-req.json")
        session_down_res_path = os.path.join(session_dir, f"{ts}-downstream-res.json")
        session_non_stream_path = os.path.join(session_dir, f"{ts}-non-stream-res.json")

    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, ban_explore)
    if model_from_body is not None:
        body["model"] = model_from_body

    max_tokens = int(body.get("max_tokens", 1024))
    messages = body.get("messages", [])
    system = body.get("system", None)
    temperature = body.get("temperature", None)
    top_p = body.get("top_p", None)
    stop_sequences = body.get("stop_sequences", None)
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")
    thinking = body.get("thinking")

    tools = _strip_task_explore_line(tools, ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    try:
        resolved = resolve_profile(upstream_config, body, PROTOCOL_ANTHROPIC_MESSAGES)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")

    _dump_json(headers_path, dict(req.headers))
    _dump_json(req_path, log_body)
    if session_req_path:
        _dump_json(session_req_path, log_body)

    if profile.get("provider") == "anthropic":
        return await _forward_anthropic_native_messages(
            body=body,
            stream=stream,
            profile=profile,
            model=model,
            req_path=req_path,
            up_res_path=up_res_path,
            down_res_path=down_res_path,
            session_req_path=session_req_path,
            session_down_res_path=session_down_res_path,
            session_non_stream_path=session_non_stream_path,
        )

    oai_messages = anthropic_messages_to_openai(messages, system)

    upstream_payload: Dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if thinking is not None:
        upstream_payload["thinking"] = thinking

    oai_tools = anthropic_tools_to_openai_tools(tools)
    if oai_tools:
        upstream_payload["tools"] = oai_tools

    oai_tool_choice = anthropic_tool_choice_to_openai(tool_choice)
    if oai_tool_choice is not None:
        upstream_payload["tool_choice"] = oai_tool_choice

    if isinstance(tool_choice, dict) and "disable_parallel_tool_use" in tool_choice:
        upstream_payload["parallel_tool_calls"] = (not bool(tool_choice["disable_parallel_tool_use"]))

    if temperature is not None:
        upstream_payload["temperature"] = temperature
    if top_p is not None:
        upstream_payload["top_p"] = top_p
    if stop_sequences is not None:
        upstream_payload["stop"] = stop_sequences
    if stream:
        upstream_payload["stream_options"] = {"include_usage": True}

    if auth_type == "codex_oauth":
        codex_chat_body: Dict[str, Any] = {
            "messages": oai_messages,
            "stream": stream,
            "max_tokens": max_tokens,
        }
        if oai_tools:
            codex_chat_body["tools"] = oai_tools
        if oai_tool_choice is not None:
            codex_chat_body["tool_choice"] = oai_tool_choice
        if temperature is not None:
            codex_chat_body["temperature"] = temperature
        if top_p is not None:
            codex_chat_body["top_p"] = top_p
        if stop_sequences is not None:
            codex_chat_body["stop"] = stop_sequences
        upstream_payload = _build_codex_responses_payload_from_chat(codex_chat_body, model)
        upstream_payload, codex_reinject_trace = _maybe_reinject_codex_reasoning(
            session_id=session_id,
            provider=str(profile.get("provider") or ""),
            model=model,
            codex_chat_body=codex_chat_body,
            codex_payload=upstream_payload,
        )
    else:
        codex_reinject_trace = None

    upstream_url = build_upstream_url(profile, PROTOCOL_ANTHROPIC_MESSAGES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await build_headers_by_profile(profile, model)

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
                        headers=hdrs,
                        request_body=upstream_payload,
                    ),
                    headers=upstream_headers,
                    max_retries=max_retries,
                    is_retryable=is_rate_limit_status,
                    refresh_headers=lambda: build_headers_by_profile(profile, model),
                )

            _dump_json(up_res_path, {"type": "codex_nonstream_bridge_capture", "chunks": result.get("chunks") or []})
            if not bool(result.get("ok")):
                down_obj = {
                    "type": "passthrough_error",
                    "status_code": int(result.get("status_code") or 500),
                    "media_type": "application/json",
                    "body": str(result.get("error_text") or ""),
                }
                _dump_json(down_res_path, down_obj)
                if session_down_res_path:
                    _discard_session_req(session_req_path)
                return Response(
                    content=result.get("error_bytes") or b"",
                    status_code=int(result.get("status_code") or 500),
                    media_type="application/json",
                )

            codex_resp_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
            _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_resp_json)
            data = _codex_responses_to_chat_completion(codex_resp_json, model)
        else:
            r = await post_with_retry(
                upstream_url=upstream_url,
                request_body=upstream_payload,
                headers=upstream_headers,
                max_retries=max_retries,
                is_retryable=is_rate_limit_status,
                refresh_headers=lambda: build_headers_by_profile(profile, model),
                verify=verify,
                timeout_seconds=timeout_seconds,
                trust_env=trust_env,
            )

            up_obj = _resp_to_obj(r)
            _dump_json(up_res_path, up_obj)

            if r.status_code >= 400:
                down_obj = {
                    "type": "passthrough_error",
                    "status_code": r.status_code,
                    "media_type": r.headers.get("content-type", "application/json"),
                    "body": (r.text if r.text is not None else ""),
                }
                _dump_json(down_res_path, down_obj)
                if session_down_res_path:
                    has_usage = False
                    try:
                        body_json = json.loads(down_obj["body"])
                        if isinstance(body_json, dict) and "usage" in body_json:
                            has_usage = True
                    except Exception:
                        pass

                    if not has_usage:
                        if session_req_path and os.path.exists(session_req_path):
                            try:
                                os.remove(session_req_path)
                            except Exception:
                                pass
                    else:
                        _dump_json(session_down_res_path, down_obj)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )

            data = r.json()
        usage = data.get("usage")
        if session_down_res_path and usage is None:
            if session_req_path and os.path.exists(session_req_path):
                try:
                    os.remove(session_req_path)
                except Exception:
                    pass
            session_down_res_path = None

        usage = usage or {}
        cache_creation = usage.get("cache_creation") or {}
        if not isinstance(cache_creation, dict):
            cache_creation = {}
        server_tool_use = usage.get("server_tool_use")
        if not isinstance(server_tool_use, dict):
            server_tool_use = {"web_search_requests": 0}
        service_tier = usage.get("service_tier") or "standard"

        choice0 = (data.get("choices") or [None])[0] or {}
        finish_reason = choice0.get("finish_reason")
        msg = choice0.get("message") or {}

        assistant_text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        content_blocks = []

        if expose_thinking:
            rc = msg.get("reasoning_content")
            if rc:
                content_blocks.append({"type": "thinking", "thinking": rc})

        if assistant_text:
            content_blocks.append({"type": "text", "text": assistant_text})

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "unknown_tool"
            tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex}"
            args_str = fn.get("arguments") or "{}"
            try:
                tool_input = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
            except Exception:
                tool_input = {"_raw_arguments": args_str}

            content_blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": tool_input,
            })

        anthropic_stop_reason = oai_finish_reason_to_stop_reason(finish_reason)
        if tool_calls and anthropic_stop_reason != "tool_use":
            anthropic_stop_reason = "tool_use"

        resp = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content_blocks,
            "stop_reason": anthropic_stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation": {
                    "ephemeral_1h_input_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0),
                    "ephemeral_5m_input_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0),
                },
                "server_tool_use": server_tool_use,
                "service_tier": service_tier,
            },
        }
        _dump_json(down_res_path, resp)
        if session_down_res_path:
            _dump_json(session_down_res_path, resp)
        return JSONResponse(resp)

    async def sse() -> AsyncIterator[bytes]:
        up_chunks = []
        down_events = []

        def emit(event: str, data: Dict[str, Any]) -> bytes:
            if isinstance(data, dict) and "type" not in data:
                data = {"type": event, **data}
            down_events.append({"event": event, "data": data})
            return _sse_event(event, data)

        msg_id = f"msg_{uuid.uuid4().hex}"

        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        usage_received = False
        final_finish_reason: Optional[str] = None

        def _update_usage_from_obj(obj: Any) -> None:
            nonlocal prompt_tokens, completion_tokens, total_tokens, usage_received
            if not isinstance(obj, dict):
                return
            u = obj.get("usage")
            if isinstance(u, dict):
                usage_received = True
                if u.get("prompt_tokens") is not None:
                    prompt_tokens = int(u.get("prompt_tokens") or prompt_tokens)
                if u.get("completion_tokens") is not None:
                    completion_tokens = int(u.get("completion_tokens") or completion_tokens)
                if u.get("total_tokens") is not None:
                    total_tokens = int(u.get("total_tokens") or total_tokens)
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                c0 = choices[0] or {}
                if isinstance(c0, dict):
                    u2 = c0.get("usage")
                    if isinstance(u2, dict):
                        usage_received = True
                        if u2.get("prompt_tokens") is not None:
                            prompt_tokens = int(u2.get("prompt_tokens") or prompt_tokens)
                        if u2.get("completion_tokens") is not None:
                            completion_tokens = int(u2.get("completion_tokens") or completion_tokens)

        current_block_index = 0
        current_block_type = None
        thinking_started = False
        text_started = False
        tool_map: Dict[int, Dict[str, Any]] = {}
        has_started = False

        try:
            if auth_type == "codex_oauth":
                async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(timeout_seconds),
                    trust_env=trust_env,
                ) as client:
                    result = await collect_codex_response_from_stream(
                        client=client,
                        upstream_url=upstream_url,
                        headers=upstream_headers,
                        request_body=upstream_payload,
                    )
                    up_chunks.extend(result.get("chunks") or [])
                    if not bool(result.get("ok")):
                        err_text = str(result.get("error_text") or "")
                        yield emit("error", {"upstream_status": int(result.get("status_code") or 500), "upstream_body": err_text})
                        yield emit("message_stop", {})
                        return

                    codex_resp_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                    _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_resp_json)
                    chat_obj = _codex_responses_to_chat_completion(codex_resp_json, model)
                    usage_obj = chat_obj.get("usage") if isinstance(chat_obj.get("usage"), dict) else {}
                    usage_received = bool(usage_obj)
                    prompt_tokens = int(usage_obj.get("prompt_tokens") or 0)
                    completion_tokens = int(usage_obj.get("completion_tokens") or 0)
                    text = (
                        (chat_obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        if isinstance(chat_obj.get("choices"), list)
                        else ""
                    )
                    tool_uses = _extract_codex_output_tool_uses(codex_resp_json)

                    yield emit("message_start", {
                        "message": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": prompt_tokens, "output_tokens": 0},
                        }
                    })
                    if text:
                        yield emit("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                        yield emit("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": text}})
                        yield emit("content_block_stop", {"index": 0})
                    if tool_uses:
                        tool_base_index = 1 if text else 0
                        for idx, tool_use in enumerate(tool_uses):
                            block_index = tool_base_index + idx
                            tool_input = tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {}
                            partial_json = json.dumps(tool_input, ensure_ascii=False)
                            yield emit(
                                "content_block_start",
                                {
                                    "index": block_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": str(tool_use.get("id") or f"toolu_{uuid.uuid4().hex}"),
                                        "name": str(tool_use.get("name") or "unknown"),
                                        "input": {},
                                    },
                                },
                            )
                            if partial_json:
                                yield emit(
                                    "content_block_delta",
                                    {
                                        "index": block_index,
                                        "delta": {"type": "input_json_delta", "partial_json": partial_json},
                                    },
                                )
                            yield emit("content_block_stop", {"index": block_index})
                    yield emit("message_delta", {
                        "delta": {"stop_reason": "tool_use" if tool_uses else "end_turn"},
                        "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
                    })
                    yield emit("message_stop", {})
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

                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=upstream_payload) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})

                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", "ignore")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            break

                        connection_established = True

                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", "ignore")
                            up_chunks.append({"type": "error_body", "text": err_text})
                            yield emit("error", {"upstream_status": r.status_code, "upstream_body": err_text})
                            yield emit("message_stop", {})
                            return

                        async for line in r.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                up_chunks.append({"type": "done"})
                                break

                            try:
                                chunk = json.loads(data_str)
                                up_chunks.append({"type": "chunk", "json": chunk})
                            except Exception:
                                up_chunks.append({"type": "chunk", "raw": data_str})
                                continue

                            _update_usage_from_obj(chunk)

                            if not has_started:
                                has_started = True
                                yield emit("message_start", {
                                    "message": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "model": chunk.get("model", model),
                                        "content": [],
                                        "stop_reason": None,
                                        "stop_sequence": None,
                                        "usage": {"input_tokens": prompt_tokens, "output_tokens": 0},
                                    }
                                })

                            choice0 = (chunk.get("choices") or [None])[0] or {}
                            delta = choice0.get("delta") or {}
                            fr = choice0.get("finish_reason")
                            if fr:
                                final_finish_reason = fr

                            rc = delta.get("reasoning_content")
                            if rc and expose_thinking:
                                if text_started:
                                    continue
                                if current_block_type is not None and current_block_type != "thinking":
                                    yield emit("content_block_stop", {"index": current_block_index})
                                    current_block_type = None

                                if current_block_type is None:
                                    if not thinking_started:
                                        current_block_index = 0
                                        thinking_started = True
                                    yield emit("content_block_start", {
                                        "index": current_block_index,
                                        "content_block": {"type": "thinking", "thinking": ""}
                                    })
                                    current_block_type = "thinking"

                                yield emit("content_block_delta", {
                                    "index": current_block_index,
                                    "delta": {"type": "thinking_delta", "thinking": rc}
                                })

                            txt = delta.get("content")
                            if txt is not None:
                                if current_block_type is not None and current_block_type != "text":
                                    yield emit("content_block_stop", {"index": current_block_index})
                                    current_block_type = None

                                if current_block_type is None:
                                    if not text_started:
                                        current_block_index = 1 if thinking_started else 0
                                        text_started = True
                                    yield emit("content_block_start", {
                                        "index": current_block_index,
                                        "content_block": {"type": "text", "text": ""}
                                    })
                                    current_block_type = "text"

                                yield emit("content_block_delta", {
                                    "index": current_block_index,
                                    "delta": {"type": "text_delta", "text": txt}
                                })

                            tcs = delta.get("tool_calls")
                            if tcs:
                                for tc in tcs:
                                    idx = tc.get("index")
                                    if idx is None:
                                        continue

                                    if idx not in tool_map:
                                        if current_block_type is not None:
                                            yield emit("content_block_stop", {"index": current_block_index})
                                            current_block_type = None

                                        if not tool_map:
                                            base_index = 0
                                            if thinking_started:
                                                base_index += 1
                                            if text_started:
                                                base_index += 1
                                            current_block_index = base_index
                                        else:
                                            current_block_index += 1

                                        t_id = tc.get("id") or f"toolu_{uuid.uuid4().hex}"
                                        fn = tc.get("function") or {}
                                        t_name = fn.get("name") or "unknown"

                                        tool_map[idx] = {
                                            "block_index": current_block_index,
                                            "id": t_id,
                                            "name": t_name
                                        }
                                        current_block_type = "tool_use"

                                        yield emit("content_block_start", {
                                            "index": current_block_index,
                                            "content_block": {
                                                "type": "tool_use",
                                                "id": t_id,
                                                "name": t_name,
                                                "input": {}
                                            }
                                        })

                                    fn = tc.get("function") or {}
                                    args = fn.get("arguments")
                                    if args:
                                        b_idx = tool_map[idx]["block_index"]
                                        yield emit("content_block_delta", {
                                            "index": b_idx,
                                            "delta": {"type": "input_json_delta", "partial_json": args}
                                        })

                        if connection_established:
                            if current_block_type is not None:
                                yield emit("content_block_stop", {"index": current_block_index})

                            stop_reason = oai_finish_reason_to_stop_reason(final_finish_reason) or "end_turn"
                            if tool_map and stop_reason != "tool_use":
                                if final_finish_reason == "tool_calls":
                                    stop_reason = "tool_use"

                            yield emit("message_delta", {
                                "delta": {"stop_reason": stop_reason},
                                "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
                            })
                            yield emit("message_stop", {})
                            return

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        retry_headers = await build_headers_by_profile(profile, model)

                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    yield emit("error", {"upstream_status": last_retry_status, "upstream_body": last_retry_err_text})
                    yield emit("message_stop", {})
                    return

        finally:
            _dump_json(up_res_path, {"type": "openai_sse_capture", "chunks": up_chunks})
            _dump_json(down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
            if session_down_res_path:
                if not usage_received:
                    _discard_session_req(session_req_path)
                else:
                    _dump_json(session_down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
                    non_stream_resp = _build_anthropic_non_stream_from_events(down_events, model)
                    usage = _extract_usage_from_obj(non_stream_resp) if non_stream_resp else None
                    if session_non_stream_path and non_stream_resp and _usage_dict_has_tokens(usage):
                        _dump_json(session_non_stream_path, non_stream_resp)

    return StreamingResponse(sse(), media_type="text/event-stream")

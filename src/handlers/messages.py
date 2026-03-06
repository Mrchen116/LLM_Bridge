from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from src.adapters.codex_oauth_adapter import collect_with_retry
from src.adapters.http_retry import post_with_retry
from src.adapters.upstream_executor import (
    build_codex_oauth_style_headers,
    build_headers_by_profile,
    collect_codex_response_from_stream,
    is_rate_limit_status,
    mark_retryable_response_for_profile,
    should_trigger_codex_failover,
)
from src.bridge.anthropic_openai import (
    anthropic_messages_to_openai_chat_messages,
    anthropic_tool_choice_to_openai_chat_tool_choice,
    anthropic_tools_to_openai_chat_tools,
    openai_chat_finish_reason_to_anthropic_stop_reason,
)
from src.bridge.openai_codex import (
    codex_response_to_openai_chat_completion,
    openai_chat_body_to_codex_payload,
)
from src.handlers.messages_stream import build_openai_bridge_streaming_response
from src.observability.turn_logging import (
    DOWNSTREAM_FORMAT_ANTHROPIC_MESSAGES,
    TurnLogPaths,
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
from proxy_converters import (
    _extract_model_and_ban_explore,
    _strip_task_explore_line,
)
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _parse_anthropic_sse_chunks_to_events,
    _resp_to_obj,
)

from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_codex_oauth_retry_attempts,
    get_effective_auth_type,
    get_runtime_options,
    resolve_profile,
)


async def _forward_anthropic_native_messages(
    *,
    stream: bool,
    model: str,
    payload: Dict[str, Any],
    upstream_url: str,
    verify: bool,
    timeout_seconds: float,
    max_retries: int,
    trust_env: bool,
    upstream_headers: Dict[str, str],
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
    profile: Dict[str, Any],
    turn_logs: TurnLogPaths,
) -> Response:
    if not stream:
        r = await post_with_retry(
            upstream_url=upstream_url,
            request_body=payload,
            headers=upstream_headers,
            max_retries=max_retries,
            is_retryable=is_rate_limit_status,
            refresh_headers=refresh_headers,
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
        )

        upstream_obj = _resp_to_obj(r)
        down_obj = {
            "type": "anthropic_passthrough_response",
            "status_code": r.status_code,
            "headers": dict(r.headers),
        }
        try:
            down_obj["json"] = r.json()
            non_stream_obj = down_obj["json"] if isinstance(down_obj["json"], dict) else down_obj
        except Exception:
            down_obj["text"] = r.text
            non_stream_obj = down_obj
        log_response_phase(
            turn_logs,
            upstream_response_obj=upstream_obj,
            downstream_response_obj=down_obj,
            non_stream_response_obj=non_stream_obj,
        )

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
                retry_headers = upstream_headers

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
                                retry_headers = await refresh_headers()
                            continue

                        connection_established = True
                        async for chunk in r.aiter_raw():
                            down_chunks.append(chunk.decode("utf-8", errors="replace"))
                            yield chunk
                        return

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await refresh_headers()

                if (not connection_established) and (last_retry_status is not None):
                    if last_retry_err_text:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    return
        finally:
            events = _parse_anthropic_sse_chunks_to_events(down_chunks)
            non_stream_resp = _build_anthropic_non_stream_from_events(events, model)
            if non_stream_resp is None:
                non_stream_resp = {
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            log_response_phase(
                turn_logs,
                upstream_response_obj={"type": "anthropic_native_sse_capture", "chunks": up_chunks},
                downstream_response_obj={"type": "anthropic_native_sse_capture", "chunks": down_chunks},
                non_stream_response_obj=non_stream_resp,
            )

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")


def _build_openai_bridge_payload(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    system: Any,
    max_tokens: int,
    stream: bool,
    thinking: Any,
    tools: Any,
    tool_choice: Any,
    temperature: Any,
    top_p: Any,
    stop_sequences: Any,
    auth_type: str,
    session_id: Optional[str],
    provider: str,
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    oai_messages = anthropic_messages_to_openai_chat_messages(messages, system)

    upstream_payload: Dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if thinking is not None:
        upstream_payload["thinking"] = thinking

    oai_tools = anthropic_tools_to_openai_chat_tools(tools)
    if oai_tools:
        upstream_payload["tools"] = oai_tools

    oai_tool_choice = anthropic_tool_choice_to_openai_chat_tool_choice(tool_choice)
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

    if auth_type != "codex_oauth":
        return upstream_payload, None

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

    codex_payload = openai_chat_body_to_codex_payload(codex_chat_body, model)
    codex_payload, codex_reinject_trace = _maybe_reinject_codex_reasoning(
        session_id=session_id,
        provider=provider,
        model=model,
        codex_chat_body=codex_chat_body,
        codex_payload=codex_payload,
    )
    return codex_payload, codex_reinject_trace


def _build_anthropic_response_from_openai_chat(
    *,
    data: Dict[str, Any],
    model: str,
    expose_thinking: bool,
) -> Dict[str, Any]:
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

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
    content_blocks: List[Dict[str, Any]] = []

    if expose_thinking:
        reasoning_content = msg.get("reasoning_content")
        if reasoning_content:
            content_blocks.append({"type": "thinking", "thinking": reasoning_content})
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
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": tool_input,
            }
        )

    stop_reason = openai_chat_finish_reason_to_anthropic_stop_reason(finish_reason)
    if tool_calls and stop_reason != "tool_use":
        stop_reason = "tool_use"

    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
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


async def _handle_openai_bridge_non_stream(
    *,
    auth_type: str,
    model: str,
    profile: Dict[str, Any],
    upstream_url: str,
    upstream_payload: Dict[str, Any],
    upstream_headers: Dict[str, str],
    max_retries: int,
    verify: bool,
    timeout_seconds: float,
    trust_env: bool,
    codex_reinject_trace: Optional[Dict[str, Any]],
    turn_logs: TurnLogPaths,
    expose_thinking: bool,
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
) -> Response:
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
                refresh_headers=refresh_headers,
                on_retryable_response=lambda hdrs, status, err: mark_retryable_response_for_profile(
                    profile=profile,
                    headers=hdrs,
                    status_code=status,
                    error_text=err,
                ),
                should_retry_result=lambda result: should_trigger_codex_failover(
                    int(result.get("status_code") or 0),
                    str(result.get("error_text") or ""),
                ),
            )

        upstream_obj = {"type": "codex_nonstream_bridge_capture", "chunks": result.get("chunks") or []}
        if not bool(result.get("ok")):
            down_obj = {
                "type": "passthrough_error",
                "status_code": int(result.get("status_code") or 500),
                "media_type": "application/json",
                "body": str(result.get("error_text") or ""),
            }
            log_response_phase(
                turn_logs,
                upstream_response_obj=upstream_obj,
                downstream_response_obj=down_obj,
                non_stream_response_obj=down_obj,
            )
            return Response(
                content=result.get("error_bytes") or b"",
                status_code=int(result.get("status_code") or 500),
                media_type="application/json",
            )

        codex_resp_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
        _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_resp_json)
        data = codex_response_to_openai_chat_completion(codex_resp_json, model)
    else:
        r = await post_with_retry(
            upstream_url=upstream_url,
            request_body=upstream_payload,
            headers=upstream_headers,
            max_retries=max_retries,
            is_retryable=is_rate_limit_status,
            refresh_headers=refresh_headers,
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
        )

        upstream_obj = _resp_to_obj(r)

        if r.status_code >= 400:
            down_obj = {
                "type": "passthrough_error",
                "status_code": r.status_code,
                "media_type": r.headers.get("content-type", "application/json"),
                "body": (r.text if r.text is not None else ""),
            }
            log_response_phase(
                turn_logs,
                upstream_response_obj=upstream_obj,
                downstream_response_obj=down_obj,
                non_stream_response_obj=down_obj,
            )
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "application/json"),
            )

        data = r.json()

    resp = _build_anthropic_response_from_openai_chat(
        data=data,
        model=model,
        expose_thinking=expose_thinking,
    )
    log_response_phase(
        turn_logs,
        upstream_response_obj=upstream_obj,
        downstream_response_obj=resp,
        non_stream_response_obj=resp,
    )
    return JSONResponse(resp)


async def run_messages_flow(
    req: Request,
    *,
    ban_stream: bool,
    ban_explore: bool,
    expose_thinking: bool,
    upstream_config: Dict[str, Any],
    logs_raw_dir: str,
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
    session_id = _extract_session_id_from_headers(req.headers) or _extract_session_id_from_body_metadata(body)

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
    upstream_url = build_upstream_url(profile, PROTOCOL_ANTHROPIC_MESSAGES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    if auth_type == "codex_oauth":
        max_retries = get_codex_oauth_retry_attempts(profile, max_retries)

    async def refresh_upstream_headers() -> Dict[str, str]:
        headers = await build_headers_by_profile(profile, model)
        if auth_type != "codex_oauth":
            return headers
        return build_codex_oauth_style_headers(
            auth_headers=headers,
            client_headers=req.headers,
            session_id=session_id,
        )

    upstream_headers = await refresh_upstream_headers()
    turn_logs = build_turn_log_paths(
        logs_raw_dir=logs_raw_dir,
        logs_session_dir=logs_session_dir,
        raw_bucket=resolve_raw_bucket(auth_type=auth_type, provider=str(profile.get("provider") or "")),
        downstream_format=DOWNSTREAM_FORMAT_ANTHROPIC_MESSAGES,
        session_id=session_id,
    )

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")

    if profile.get("provider") == "anthropic":
        upstream_payload = dict(body)
        upstream_payload["model"] = model
        log_request_phase(
            turn_logs,
            request_obj=log_body,
            upstream_request_obj=upstream_payload,
            client_headers=req.headers,
            upstream_headers=upstream_headers,
        )
        return await _forward_anthropic_native_messages(
            stream=stream,
            model=model,
            payload=upstream_payload,
            upstream_url=upstream_url,
            verify=verify,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            trust_env=trust_env,
            upstream_headers=upstream_headers,
            refresh_headers=refresh_upstream_headers,
            profile=profile,
            turn_logs=turn_logs,
        )

    upstream_payload, codex_reinject_trace = _build_openai_bridge_payload(
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        stream=stream,
        thinking=thinking,
        tools=tools,
        tool_choice=tool_choice,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
        auth_type=auth_type,
        session_id=session_id,
        provider=str(profile.get("provider") or ""),
    )

    log_request_phase(
        turn_logs,
        request_obj=log_body,
        upstream_request_obj=upstream_payload,
        client_headers=req.headers,
        upstream_headers=upstream_headers,
    )

    if not stream:
        return await _handle_openai_bridge_non_stream(
            auth_type=auth_type,
            model=model,
            profile=profile,
            upstream_url=upstream_url,
            upstream_payload=upstream_payload,
            upstream_headers=upstream_headers,
            max_retries=max_retries,
            verify=verify,
            timeout_seconds=timeout_seconds,
            trust_env=trust_env,
            codex_reinject_trace=codex_reinject_trace,
            turn_logs=turn_logs,
            expose_thinking=expose_thinking,
            refresh_headers=refresh_upstream_headers,
        )

    return build_openai_bridge_streaming_response(
        auth_type=auth_type,
        model=model,
        profile=profile,
        upstream_url=upstream_url,
        upstream_payload=upstream_payload,
        upstream_headers=upstream_headers,
        refresh_headers=refresh_upstream_headers,
        max_retries=max_retries,
        verify=verify,
        timeout_seconds=timeout_seconds,
        trust_env=trust_env,
        expose_thinking=expose_thinking,
        codex_reinject_trace=codex_reinject_trace,
        turn_logs=turn_logs,
    )

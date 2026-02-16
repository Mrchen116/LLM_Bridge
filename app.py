import os
import json
import time
import uuid
import asyncio
import glob
from typing import Any, Dict, List, Optional, AsyncIterator, Tuple

import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_runtime_options,
    get_effective_auth_type,
    load_and_validate_config,
    resolve_profile,
)
load_dotenv(override=True)
import logging

from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _codex_responses_to_chat_completion,
    _extract_codex_output_tool_uses,
    _extract_model_and_ban_explore,
    _strip_task_explore_line,
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai_tools,
    calculate_token_count,
    oai_finish_reason_to_stop_reason,
)
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _collect_usage_tokens,
    _discard_session_req,
    _dump_json,
    _extract_usage_from_obj,
    _parse_anthropic_sse_chunks_to_events,
    _resp_to_obj,
    _should_skip_session_logging,
    _sse_event,
    _usage_dict_has_tokens,
)
from src.orchestrator.reasoning_reinject import (
    _extract_response_completed_object_from_sse_chunks,
    _extract_session_id_from_body_metadata,
    _maybe_reinject_codex_reasoning,
    _maybe_reinject_codex_reasoning_for_responses,
    _update_codex_reasoning_reinject_cache,
    _update_codex_reasoning_reinject_cache_for_responses,
)
from src.adapters.upstream_executor import (
    build_headers_by_profile as _build_headers_by_profile,
    collect_codex_response_from_stream as _collect_codex_response_from_stream,
    is_rate_limit_status,
)
from src.ingress.responses import handle_openai_responses

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
UPSTREAM_CONFIG = load_and_validate_config()

LOGS_ROOT_DIR = "logs"
LOGS_OPENAI_DIR = os.path.join(LOGS_ROOT_DIR, "openai")
LOGS_ANTHROPIC_DIR = os.path.join(LOGS_ROOT_DIR, "anthropic")
LOGS_SESSION_DIR = os.path.join(LOGS_ROOT_DIR, "session")
LOGS_CODEAGENT_DIR = os.path.join(LOGS_ROOT_DIR, "codeagent")


app = FastAPI(title="Anthropic+OpenAI Proxy (FastAPI)")


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True}


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
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            r = None
            last_retry_response = None
            headers = await _build_headers_by_profile(profile, model)

            for attempt in range(max_retries):
                r = await client.post(upstream_url, headers=headers, json=payload)
                if not is_rate_limit_status(r.status_code):
                    break
                last_retry_response = r
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    headers = await _build_headers_by_profile(profile, model)

            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

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
                retry_headers = await _build_headers_by_profile(profile, model)

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
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        connection_established = True
                        async for chunk in r.aiter_raw():
                            down_chunks.append(chunk.decode("utf-8", errors="replace"))
                            yield chunk
                        return

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)

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


@app.post("/v1/responses")
async def openai_responses(req: Request):
    return await handle_openai_responses(req)


@app.post("/v1/messages")
async def v1_messages(req: Request):
    body = await req.json()
    # 临时禁用流式请求：直接返回错误，不记录任何日志
    body_stream = bool(body.get("stream", False))
    header_stream = req.headers.get("x-stainless-helper-method", "").lower() == "stream"
    stream = body_stream or header_stream
    if stream and BAN_STREAM:
        return JSONResponse(
            {
                "error": {
                    "message": "暂不支持流式请求，请使用非流式模式重试",
                    "type": "stream_disabled",
                }
            },
            status_code=400,
        )
    os.makedirs(LOGS_ANTHROPIC_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名

    session_id = None
    session_id = _extract_session_id_from_body_metadata(body)
    skip_session_logging = False
    if session_id:
        skip_session_logging = _should_skip_session_logging(body)

    req_path = os.path.join(LOGS_ANTHROPIC_DIR, f"{ts}-req.json")
    up_res_path = os.path.join(LOGS_ANTHROPIC_DIR, f"{ts}-upstream-res.json")
    down_res_path = os.path.join(LOGS_ANTHROPIC_DIR, f"{ts}-downstream-res.json")
    headers_path = os.path.join(LOGS_ANTHROPIC_DIR, f"{ts}-headers.json")

    session_req_path = None
    session_down_res_path = None
    session_non_stream_path = None
    if session_id and not skip_session_logging:
        # 若该 session_id 已有目录则复用，否则按当前时间戳新建
        os.makedirs(LOGS_SESSION_DIR, exist_ok=True)
        existing_dirs = sorted(glob.glob(os.path.join(LOGS_SESSION_DIR, f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join(LOGS_SESSION_DIR, f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        session_req_path = os.path.join(session_dir, f"{ts}-req.json")
        session_down_res_path = os.path.join(session_dir, f"{ts}-downstream-res.json")
        session_non_stream_path = os.path.join(session_dir, f"{ts}-non-stream-res.json")

    # ---- 解析 model & ban_explore ----
    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, BAN_EXPLORE)
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

    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(tools, ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    try:
        resolved = resolve_profile(UPSTREAM_CONFIG, body, PROTOCOL_ANTHROPIC_MESSAGES)
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
        # codex_oauth 的上游是 responses 端点，不接受 chat/completions 风格 payload。
        # 这里把「已转好的 OpenAI chat 消息」再桥接成 responses 请求，自动补 instructions/store/include。
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
    upstream_headers = await _build_headers_by_profile(profile, model)

    # ---- non-stream ----
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
                        request_body=upstream_payload,
                    )
                    status_code = int(result.get("status_code") or 0)
                    if not is_rate_limit_status(status_code):
                        break
                    last_retry_result = result
                    err_msg = result.get("error_text") or ""
                    logging.warning(f"{attempt} retryable response (messages codex non-stream): {status_code} {err_msg}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)
                if is_rate_limit_status(int(result.get("status_code") or 0)) and last_retry_result is not None:
                    result = last_retry_result

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
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                # 限流状态码重试逻辑：重试次数来自 profile 配置
                r = None
                last_retry_response = None

                for attempt in range(max_retries):
                    r = await client.post(upstream_url, headers=upstream_headers, json=upstream_payload)

                    if not is_rate_limit_status(r.status_code):
                        # 不是限流错误，直接使用这个响应
                        break

                    # 是限流错误，保存响应用于最后返回
                    last_retry_response = r
                    logging.warning(f"{attempt} retryable response: {r.status_code} {r.text}")
                    # 如果不是最后一次重试，等待后继续
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        upstream_headers = await _build_headers_by_profile(profile, model)

                # 如果所有重试都是限流错误，使用最后一次的响应
                if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                    r = last_retry_response

            # 1) 存 upstream 原始返回（成功/失败都存）
            up_obj = _resp_to_obj(r)
            _dump_json(up_res_path, up_obj)

            # upstream 错误：下游是透传 Response，这个也存一份“下游实际返回长啥样”
            if r.status_code >= 400:
                down_obj = {
                    "type": "passthrough_error",
                    "status_code": r.status_code,
                    "media_type": r.headers.get("content-type", "application/json"),
                    "body": (r.text if r.text is not None else ""),
                }
                _dump_json(down_res_path, down_obj)
                if session_down_res_path:
                    # 检查是否包含 usage 字段 (有些错误返回也会带 usage，如果有则存，没有则不存)
                    has_usage = False
                    try:
                        body_json = json.loads(down_obj["body"])
                        if isinstance(body_json, dict) and "usage" in body_json:
                            has_usage = True
                    except:
                        pass

                    if not has_usage:
                        # 如果不包含 usage，则不写入 res 到 session，且把已写入的 req 也删掉
                        if session_req_path and os.path.exists(session_req_path):
                            try:
                                os.remove(session_req_path)
                            except:
                                pass
                    else:
                        _dump_json(session_down_res_path, down_obj)
                return Response(
                    content=r.content,
                    status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                )

            # 2) 正常：整理为 Anthropic message 返回
            data = r.json()
        usage = data.get("usage")
        if session_down_res_path and usage is None:
            # 如果没有 usage 字段，则认为是不正常返回，不存 session，且把已写入的 req 也删掉
            if session_req_path and os.path.exists(session_req_path):
                try:
                    os.remove(session_req_path)
                except:
                    pass
            # 标记为 None，后续不再写入 session_down_res_path
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

        # 1) thinking（如果开启且上游有 reasoning_content）
        if EXPOSE_THINKING:
            rc = msg.get("reasoning_content")
            if rc:
                content_blocks.append({"type": "thinking", "thinking": rc})

        # 2) final text
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

        # 重新映射 stop_reason
        anthropic_stop_reason = oai_finish_reason_to_stop_reason(finish_reason)
        # 如果有 tool_calls，通常强制为 tool_use
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

    # ---- stream SSE ----
    async def sse() -> AsyncIterator[bytes]:
        up_chunks = []    # upstream chunks
        down_events = []  # downstream events

        def emit(event: str, data: Dict[str, Any]) -> bytes:
            # Anthropic SSE: data 里需要带 type 字段（和 event 同名）
            if isinstance(data, dict) and "type" not in data:
                data = {"type": event, **data}
            down_events.append({"event": event, "data": data})
            return _sse_event(event, data)

        msg_id = f"msg_{uuid.uuid4().hex}"

        # Usage tracking
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
            # Some providers put usage in choices
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

        # State machine for content blocks
        # 目标：与 Anthropic 正常语义一致（thinking=0, text=1，避免跳号）
        current_block_index = 0
        current_block_type = None  # "thinking" | "text" | "tool_use"
        thinking_started = False
        text_started = False
        tool_map: Dict[int, Dict[str, Any]] = {}  # openai_tool_index -> {block_index, id, name}
        has_started = False

        try:
            if auth_type == "codex_oauth":
                # responses 原生 SSE 与 chat/completions 增量格式不同，这里直接消费上游流并桥接成 Anthropic SSE。
                # 注意：codex/responses 端点在流式请求场景要求 stream=true，不能降级成非流式调用。
                async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(timeout_seconds),
                    trust_env=trust_env,
                ) as client:
                    result = await _collect_codex_response_from_stream(
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
                    # codex_oauth 分支同样要标记 usage 已收到，否则 finally 会误判为异常并清空 session 请求日志。
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
                            # Anthropic 客户端通常依赖 input_json_delta 聚合 tool_use 参数，
                            # 仅在 content_block_start 里放完整 input 可能被部分客户端忽略。
                            # 这里统一按增量事件输出，保证与原生行为兼容。
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
                # 限流状态码重试逻辑：重试次数来自 profile 配置
                last_retry_err_text = None
                last_retry_status = None
                retry_headers = upstream_headers
                connection_established = False
                
                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=upstream_payload) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        
                        if is_rate_limit_status(r.status_code):
                            # 是限流错误，保存错误信息并关闭连接
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", "ignore")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            # 关闭连接，准备重试（退出async with块）
                            break
                        
                        # 不是限流错误，继续在这个连接上处理
                        connection_established = True
                        
                        # 处理其他错误（非406）
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", "ignore")
                            up_chunks.append({"type": "error_body", "text": err_text})
                            yield emit("error", {"upstream_status": r.status_code, "upstream_body": err_text})
                            yield emit("message_stop", {})
                            return

                        # 正常情况，读取流数据
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
                            
                            # Send message_start on first chunk
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

                            # 0. Reasoning Content
                            rc = delta.get("reasoning_content")
                            if rc and EXPOSE_THINKING:
                                # 如果已经进入文本块，后续 reasoning_content 忽略以保持索引稳定
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

                            # 1. Text Content
                            txt = delta.get("content")
                            if txt is not None:
                                # If we were in tool mode or thinking mode, or this is first block
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

                            # 2. Tool Calls
                            tcs = delta.get("tool_calls")
                            if tcs:
                                for tc in tcs:
                                    idx = tc.get("index")
                                    if idx is None:
                                        continue
                                    
                                    # Check if new tool or existing
                                    if idx not in tool_map:
                                        # New tool call -> start new block
                                        if current_block_type is not None:
                                            yield emit("content_block_stop", {"index": current_block_index})
                                            current_block_type = None
                                        
                                        # tool_use 的起始索引应在 thinking/text 之后
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
                                    
                                    # Tool arguments
                                    fn = tc.get("function") or {}
                                    args = fn.get("arguments")
                                    if args:
                                        b_idx = tool_map[idx]["block_index"]
                                        yield emit("content_block_delta", {
                                            "index": b_idx,
                                            "delta": {"type": "input_json_delta", "partial_json": args}
                                        })

                        # Cleanup：只有在成功建立连接时才发送 message_stop 并 return
                        if connection_established:
                            if current_block_type is not None:
                                yield emit("content_block_stop", {"index": current_block_index})
                            
                            stop_reason = oai_finish_reason_to_stop_reason(final_finish_reason) or "end_turn"
                            # If we had tool calls, force tool_use as stop reason if not already
                            if tool_map and stop_reason != "tool_use":
                                # Only if the finish reason wasn't explicitly something else like error/length
                                if final_finish_reason == "tool_calls":
                                    stop_reason = "tool_use"

                            yield emit("message_delta", {
                                "delta": {"stop_reason": stop_reason},
                                "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
                            })
                            yield emit("message_stop", {})
                            return
                    
                    # 如果不是最后一次重试且连接失败（限流错误），等待后继续
                    if not connection_established and attempt < max_retries - 1:
                        # 指数退避：0.1s, 0.2s, 0.4s, 0.8s
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)
                
                # 如果所有重试都是限流错误，返回错误
                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    yield emit("error", {"upstream_status": last_retry_status, "upstream_body": last_retry_err_text})
                    yield emit("message_stop", {})
                    return

        finally:
            _dump_json(up_res_path, {"type": "openai_sse_capture", "chunks": up_chunks})
            _dump_json(down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
            if session_down_res_path:
                if not usage_received:
                    # 如果没有收到 usage 字段，则认为是不正常返回，不存 session，且把已写入的 req 也删掉
                    _discard_session_req(session_req_path)
                else:
                    _dump_json(session_down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
                    non_stream_resp = _build_anthropic_non_stream_from_events(down_events, model)
                    usage = _extract_usage_from_obj(non_stream_resp) if non_stream_resp else None
                    if session_non_stream_path and non_stream_resp and _usage_dict_has_tokens(usage):
                        _dump_json(session_non_stream_path, non_stream_resp)

    return StreamingResponse(sse(), media_type="text/event-stream")
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}, status_code=400)

    messages = body.get("messages", [])
    system = body.get("system")
    tools = body.get("tools")

    token_count = calculate_token_count(messages, system, tools)

    return {"input_tokens": token_count}


@app.get("/session/{session_id}/stats")
async def session_stats(session_id: str):
    """
    返回指定 session_id 的 token 统计：
    - input_tokens：所有 *res.json 的 input_tokens 之和
    - output_tokens：所有 *res.json 的 output_tokens 之和
    - num_turns：*res.json 文件数
    """
    def _scan_session_dirs(session_dirs: List[str]) -> Dict[str, int]:
        total_input = 0
        total_output = 0
        num_turns = 0
        for d in session_dirs:
            for fp in glob.glob(os.path.join(d, "*res.json")):
                num_turns += 1
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                in_tok, out_tok = _collect_usage_tokens(data)
                total_input += in_tok
                total_output += out_tok
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "num_turns": num_turns,
        }

    session_dirs = sorted(glob.glob(os.path.join(LOGS_SESSION_DIR, f"*_{session_id}")))
    stats = _scan_session_dirs(session_dirs) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        # session 日志无数据时再回退到 codeagent 日志
        session_dirs = sorted(glob.glob(os.path.join(LOGS_CODEAGENT_DIR, f"*_{session_id}")))
        stats = _scan_session_dirs(session_dirs) if session_dirs else None

    if not stats or stats.get("num_turns", 0) == 0:
        return JSONResponse(
            {"error": f"session_id {session_id} not found"},
            status_code=404,
        )

    return {
        "session_id": session_id,
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
        "num_turns": stats.get("num_turns", 0),
    }


# ---------- OpenAI Chat Completions ----------
@app.post("/v1/chat/completions")
async def openai_chat_completions(req: Request):
    """
    OpenAI-compatible endpoint:
      - non-stream: upstream JSON pass-through
      - stream: upstream OpenAI SSE pass-through
    """
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

    ## 适配codeagent获取session id
    session_id = req.headers.get("X-Session-Id")

    # 保存请求/响应日志（OpenAI 直通）
    if session_id:
        os.makedirs(LOGS_CODEAGENT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名
        # 若该 session_id 已有目录则复用，否则按当前时间戳新建
        existing_dirs = sorted(glob.glob(os.path.join(LOGS_CODEAGENT_DIR, f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join(LOGS_CODEAGENT_DIR, f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        req_path = os.path.join(session_dir, f"{ts}-req.json")
        res_path = os.path.join(session_dir, f"{ts}--res.json")
    else:
        os.makedirs(LOGS_OPENAI_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名
        req_path = os.path.join(LOGS_OPENAI_DIR, f"{ts}-req.json")
        res_path = os.path.join(LOGS_OPENAI_DIR, f"{ts}--res.json")

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_CHAT)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await _build_headers_by_profile(profile, model)

    # 默认 model：优先用户请求体
    body["model"] = model

    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    upstream_request_body = body
    codex_chat_body: Optional[Dict[str, Any]] = None
    codex_reinject_trace: Optional[Dict[str, Any]] = None
    if auth_type == "codex_oauth":
        # OpenAI Chat -> Codex Responses 桥接入口：
        # 在转换函数里会处理 reasoning_effort 映射与 include(encrypted_content) 合并。
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

    # ---- non-stream ----
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
                # 转换失败时尽量透传聚合后的 response 对象，避免直接丢失上游信息。
                fallback_obj = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                return JSONResponse(content=fallback_obj, status_code=200)

        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            # 限流状态码重试逻辑：重试次数来自 profile 配置
            r = None
            last_retry_response = None

            for attempt in range(max_retries):
                if auth_type == "codex_oauth":
                    r = await client.post(upstream_url, headers=upstream_headers, json=upstream_request_body)
                else:
                    r = await client.post(upstream_url, headers=upstream_headers, json=body)

                if not is_rate_limit_status(r.status_code):
                    # 不是限流错误，直接使用这个响应
                    break

                # 是限流错误，保存响应用于最后返回
                last_retry_response = r
                logging.warning(f"{attempt} retryable response (chat/completions non-stream): {r.status_code} {r.text}")
                # 如果不是最后一次重试，等待后继续
                if attempt < max_retries - 1:
                    # 指数退避：1s, 2s, 4s, 8s
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await _build_headers_by_profile(profile, model)

            # 如果所有重试都是限流错误，使用最后一次的响应
            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        # 记录上下游响应（非流式）
        _dump_json(res_path, _resp_to_obj(r))

        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                codex_json = r.json()
                # 当前桥接策略：Responses 回包转回 Chat 时只保留文本与 usage。
                # output[].encrypted_content 不在 chat 标准字段中，暂不向下游暴露。
                converted = _codex_responses_to_chat_completion(codex_json, model)
                _dump_json(res_path, {"status_code": r.status_code, "headers": dict(r.headers), "json": converted})
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                # 转换失败时回退到透传，便于排查
                pass

        # ✅ 上游错误透传（状态码+body 原样返回）
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (OpenAI SSE pass-through) ----
    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        try:
            if auth_type == "codex_oauth":
                # Codex responses SSE 事件模型与 chat.completions 不同，这里做流式桥接。
                # 上游要求 stream=true，因此直接消费上游事件并转换为 OpenAI chunk。
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
                # 限流状态码重试逻辑：重试次数来自 profile 配置
                last_retry_err_text = None
                last_retry_status = None
                retry_headers = upstream_headers
                connection_established = False

                # 用于跟踪流式响应的数据
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
                            # 是限流错误，保存错误信息并关闭连接
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                            logging.warning(
                                f"{attempt} retryable response (chat/completions stream): {r.status_code} {last_retry_err_text}")
                            # 关闭连接，准备重试（进行下一次for循环）
                            if attempt < max_retries - 1:
                                # 指数退避
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        # 不是限流错误，继续在这个连接上处理
                        connection_established = True

                        # 处理其他错误（非406）
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            up_chunks.append({"type": "error_body", "body": err_text})
                            # 返回符合OpenAI格式的错误响应
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

                            # 如果是JSON格式的错误响应，尝试解析并包含详细信息
                            try:
                                error_json = json.loads(err_text)
                                if isinstance(error_json, dict):
                                    error_data["error"] = error_json
                            except:
                                # 如果不是有效的JSON，直接作为消息返回
                                error_data["error"] = {
                                    "message": err_text,
                                    "type": "upstream_error",
                                    "code": r.status_code
                                }

                            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return

                        # 正常情况，读取流数据
                        async for line in r.aiter_lines():
                            if not line:
                                continue

                            # 添加到 chunks 日志中
                            up_chunks.append(line)

                            # 检查是否是 SSE 数据行
                            if line.startswith("data:"):
                                data_part = line[5:].strip()  # 移除 "data:" 前缀

                                # 检查是否是结束标记
                                if data_part == "[DONE]":
                                    # 只有当我们收到了有效内容时才发送 DONE 标记
                                    if has_valid_content:
                                        yield b"data: [DONE]\n\n"
                                    break

                                # 尝试解析 JSON 数据
                                try:
                                    chunk_data = json.loads(data_part)

                                    # 检查是否有有效的内容
                                    choices = chunk_data.get("choices", [])
                                    if choices and len(choices) > 0:
                                        choice = choices[0]
                                        delta = choice.get("delta", {})
                                        content = delta.get("content")
                                        reasoning_content = delta.get("reasoning_content")
                                        reasoning = delta.get("reasoning")
                                        tool_calls = delta.get("tool_calls")
                                        finish_reason = choice.get("finish_reason")

                                        # 检查是否有任何有效内容（content 或 reasoning_content 或 tool_calls）
                                        # 分别处理每个字段，避免使用 elif 导致某些字段被忽略
                                        if content is not None and content != "":
                                            has_valid_content = True
                                            content_buffer += content
                                        elif content is not None and content == "" and len(content_buffer) > 0:
                                            # 空字符串但前面有内容，也认为是有效的
                                            has_valid_content = True
                                        if reasoning_content is not None and reasoning_content != "":
                                            has_valid_content = True
                                            content_buffer += reasoning_content
                                        if reasoning is not None and reasoning != "":
                                            has_valid_content = True
                                            content_buffer += reasoning
                                        if tool_calls is not None and len(tool_calls) > 0:
                                            has_valid_content = True

                                        # 如果有 finish_reason，也标记为有效
                                        if finish_reason is not None:
                                            has_valid_content = True

                                    # 适配CodeaAgent代码：只在有finish_reason时保留usage
                                    # 移除中间chunk的usage信息，避免被CodeAgent代码误判
                                    if "usage" in chunk_data:
                                        choices = chunk_data.get("choices", [])
                                        has_finish_reason = False
                                        if choices and len(choices) > 0:
                                            finish_reason = choices[0].get("finish_reason")
                                            if finish_reason is not None:
                                                has_finish_reason = True

                                        # 如果没有finish_reason，移除usage字段
                                        if not has_finish_reason:
                                            chunk_data.pop("usage", None)

                                    # 检查是否有工具调用完成
                                    should_emit_tool_calls = False
                                    if finish_reason == "tool_calls" and choices:
                                        # 检查是否有任何tool_calls（哪怕空数组）
                                        tool_calls_flat = []
                                        for choice in choices:
                                            if isinstance(choice, dict):
                                                delta = choice.get("delta", {})
                                                tcs = delta.get("tool_calls", [])
                                                if isinstance(tcs, list):
                                                    tool_calls_flat.extend(tcs)

                                        # 如果有tool_calls（哪怕是空数组）且之前已经有tool_calls记录，则触发返回
                                        if tool_calls_flat or has_valid_content:
                                            should_emit_tool_calls = True

                                    # 传递处理后的数据行
                                    yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n".encode("utf-8")

                                    # 如果检测到tool_calls完成，立即发送[i/]D[/i]标记并结束流
                                    if should_emit_tool_calls:
                                        # 确保所有tool calls都已经输出
                                        if has_valid_content:
                                            # 发送最终的有效内容块以触发tool calls处理
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

                                        # 立即发送DONE标记以结束流
                                        yield b"data: [DONE]\n\n"
                                        return
                                except json.JSONDecodeError:
                                    # 如果不是有效的 JSON，直接传递
                                    yield line.encode("utf-8") + b"\n\n"
                            else:
                                # 对于非数据行，直接传递
                                yield line.encode("utf-8") + b"\n"

                    # 如果已经成功建立连接且完成流式传输，则不再重试
                    if connection_established:
                        # 如果我们从未收到有效内容，添加一个最终的空内容块以防止客户端挂起
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

                        # 确保发送 DONE 标记
                        yield b"data: [DONE]\n\n"
                        break

                    # 如果不是最后一次重试且连接失败（406/429），等待后继续
                    if not connection_established and attempt < max_retries - 1:
                        # 指数退避
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)

                # 如果所有重试都是限流错误，返回最后一次错误
                if not connection_established and last_retry_status is not None and is_rate_limit_status(
                        last_retry_status):
                    # 直接把错误原样吐回（客户端一般也能看到）
                    if last_retry_err_text is not None:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    # 确保发送 DONE 标记
                    yield b"data: [DONE]\n\n"
                    return
        finally:
            # 无论正常/异常/客户端断开，尽最大努力落盘
            _dump_json(res_path, {"type": "openai_passthrough_sse_capture", "chunks": up_chunks})

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")

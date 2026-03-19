from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional

import httpx
from fastapi.responses import Response, StreamingResponse

from src.adapters.codex_oauth_adapter import collect_with_retry
from src.adapters.upstream_executor import (
    build_upstream_request_kwargs,
    collect_codex_response_from_stream,
    is_rate_limit_status,
    mark_retryable_response_for_profile,
    should_retry_codex_result,
    should_trigger_codex_failover,
)
from src.bridge.anthropic_openai import openai_chat_finish_reason_to_anthropic_stop_reason
from src.bridge.openai_codex import (
    codex_response_extract_tool_uses,
    codex_response_to_openai_chat_completion,
)
from src.observability.turn_logging import TurnLogPaths, log_response_phase
from src.reasoning.reinject import _update_codex_reasoning_reinject_cache
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _sse_event,
)


async def _prepend_stream_chunk(first_chunk: bytes, iterator: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    yield first_chunk
    async for chunk in iterator:
        yield chunk


async def build_openai_bridge_streaming_response(
    *,
    auth_type: str,
    model: str,
    profile: Dict[str, Any],
    upstream_url: str,
    upstream_payload: Dict[str, Any],
    upstream_headers: Dict[str, str],
    refresh_headers: Callable[[], Awaitable[Dict[str, str]]],
    max_retries: int,
    max_failovers: int,
    verify: bool,
    timeout_seconds: float,
    trust_env: bool,
    expose_thinking: bool,
    codex_reinject_trace: Optional[Dict[str, Any]],
    turn_logs: TurnLogPaths,
 ) -> Response:
    stream_result: Dict[str, Any] = {}

    async def sse() -> AsyncIterator[bytes]:
        up_chunks = []
        down_events = []
        downstream_response_obj: Dict[str, Any] = {"type": "anthropic_sse_capture", "events": down_events}
        non_stream_resp: Optional[Dict[str, Any]] = None

        def emit(event: str, data: Dict[str, Any]) -> bytes:
            if isinstance(data, dict) and "type" not in data:
                data = {"type": event, **data}
            down_events.append({"event": event, "data": data})
            return _sse_event(event, data)

        def set_error_response(*, status_code: int, body: str, media_type: str = "application/json") -> None:
            nonlocal downstream_response_obj, non_stream_resp
            error_obj = {
                "type": "passthrough_error",
                "status_code": status_code,
                "media_type": media_type,
                "body": body,
            }
            downstream_response_obj = error_obj
            non_stream_resp = error_obj
            stream_result["error_response"] = error_obj

        def error_body_json(message: str, error_type: str, code: Optional[int] = None) -> str:
            err_obj: Dict[str, Any] = {"message": message, "type": error_type}
            if code is not None:
                err_obj["code"] = code
            return json.dumps({"error": err_obj}, ensure_ascii=False)

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
                    result = await collect_with_retry(
                        collect_once=lambda hdrs: collect_codex_response_from_stream(
                            client=client,
                            upstream_url=upstream_url,
                            profile=profile,
                            headers=hdrs,
                            request_body=upstream_payload,
                        ),
                        headers=upstream_headers,
                        max_retries=max_retries,
                        max_failovers=max_failovers,
                        is_retryable=is_rate_limit_status,
                        refresh_headers=refresh_headers,
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
                        upstream_status = int(result.get("status_code") or 0)
                        is_connection_error = any(
                            isinstance(chunk, dict) and chunk.get("type") == "transport_error"
                            for chunk in (result.get("chunks") or [])
                        )
                        set_error_response(
                            status_code=502 if is_connection_error or upstream_status <= 0 else upstream_status,
                            body=error_body_json(
                                err_text or "上游返回异常状态，但响应体为空",
                                "upstream_connection_error" if is_connection_error else "upstream_http_error",
                                502 if is_connection_error or upstream_status <= 0 else upstream_status,
                            ),
                        )
                        return

                    codex_resp_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
                    _update_codex_reasoning_reinject_cache(codex_reinject_trace, codex_resp_json)
                    chat_obj = codex_response_to_openai_chat_completion(codex_resp_json, model)
                    usage_obj = chat_obj.get("usage") if isinstance(chat_obj.get("usage"), dict) else {}
                    usage_received = bool(usage_obj)
                    prompt_tokens = int(usage_obj.get("prompt_tokens") or 0)
                    completion_tokens = int(usage_obj.get("completion_tokens") or 0)
                    text = (
                        (chat_obj.get("choices") or [{}])[0].get("message", {}).get("content", "")
                        if isinstance(chat_obj.get("choices"), list)
                        else ""
                    )
                    tool_uses = codex_response_extract_tool_uses(codex_resp_json)
                    if not text and not tool_uses:
                        set_error_response(
                            status_code=502,
                            body=error_body_json("上游返回成功，但未生成任何文本或工具调用", "upstream_empty_stream"),
                        )
                        return

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
                    request_headers, request_kwargs = build_upstream_request_kwargs(
                        profile=profile,
                        headers=retry_headers,
                        request_body=upstream_payload,
                    )
                    async with client.stream("POST", upstream_url, headers=request_headers, **request_kwargs) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})

                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", "ignore")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            await mark_retryable_response_for_profile(
                                profile=profile,
                                headers=retry_headers,
                                status_code=r.status_code,
                                error_text=last_retry_err_text,
                            )
                            break

                        connection_established = True

                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", "ignore")
                            up_chunks.append({"type": "error_body", "text": err_text})
                            set_error_response(
                                status_code=r.status_code,
                                body=error_body_json(
                                    err_text or "上游返回异常状态，但响应体为空",
                                    "upstream_http_error",
                                    r.status_code,
                                ),
                            )
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
                                if txt == "" and not text_started:
                                    pass
                                else:
                                    if current_block_type is not None and current_block_type != "text":
                                        yield emit("content_block_stop", {"index": current_block_index})
                                        current_block_type = None

                                    if current_block_type is None:
                                        if not text_started:
                                            current_block_index = 1 if thinking_started else 0
                                            text_started = True
                                        elif tool_map:
                                            # 如果正在处理工具调用，则使用工具调用块的索引+1作为文本块的索引
                                            current_block_index = max(m["block_index"] for m in tool_map.values()) + 1
                                        yield emit("content_block_start", {
                                            "index": current_block_index,
                                            "content_block": {"type": "text", "text": ""}
                                        })
                                        current_block_type = "text"

                                    yield emit("content_block_delta", {
                                        "index": current_block_index,
                                        "delta": {"type": "text_delta", "text": txt}
                                    })

                                    if not text_started:
                                        text_started = True


                            tool_calls = delta.get("tool_calls")
                            if tool_calls:
                                for tc in tool_calls:
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
                            if not down_events:
                                set_error_response(
                                    status_code=502,
                                    body=error_body_json("上游建立了流式连接，但未返回任何事件", "upstream_empty_stream"),
                                )
                                return
                            if current_block_type is not None:
                                yield emit("content_block_stop", {"index": current_block_index})

                            stop_reason = openai_chat_finish_reason_to_anthropic_stop_reason(final_finish_reason) or "end_turn"
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
                        retry_headers = await refresh_headers()

                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    set_error_response(
                        status_code=last_retry_status,
                        body=error_body_json(
                            last_retry_err_text or "上游返回异常状态，但响应体为空",
                            "upstream_http_error",
                            last_retry_status,
                        ),
                    )
                    return
                if not connection_established:
                    set_error_response(
                        status_code=502,
                        body=error_body_json("上游流式请求未建立连接", "upstream_no_response"),
                    )
                    return
        except httpx.HTTPError as e:
            set_error_response(
                status_code=502,
                body=error_body_json(f"{type(e).__name__}: {e}", "upstream_connection_error"),
            )

        finally:
            if non_stream_resp is None:
                non_stream_resp = _build_anthropic_non_stream_from_events(down_events, model)
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
                upstream_response_obj={"type": "openai_sse_capture", "chunks": up_chunks},
                downstream_response_obj=downstream_response_obj,
                non_stream_response_obj=non_stream_resp,
            )

    iterator = sse()
    try:
        first_chunk = await anext(iterator)
    except StopAsyncIteration:
        error_obj = stream_result.get("error_response") or {
            "status_code": 502,
            "media_type": "application/json",
            "body": json.dumps({"error": {"message": "上游流式请求未返回任何内容", "type": "upstream_empty_stream"}}, ensure_ascii=False),
        }
        return Response(
            content=str(error_obj.get("body") or ""),
            status_code=int(error_obj.get("status_code") or 502),
            media_type=str(error_obj.get("media_type") or "application/json"),
        )
    return StreamingResponse(_prepend_stream_chunk(first_chunk, iterator), media_type="text/event-stream")

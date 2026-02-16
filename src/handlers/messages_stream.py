from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi.responses import StreamingResponse

from src.adapters.upstream_executor import (
    build_headers_by_profile,
    collect_codex_response_from_stream,
    is_rate_limit_status,
)
from src.bridge.anthropic_openai import openai_chat_finish_reason_to_anthropic_stop_reason
from src.bridge.openai_codex import (
    codex_response_extract_tool_uses,
    codex_response_to_openai_chat_completion,
)
from src.reasoning.reinject import _update_codex_reasoning_reinject_cache
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _discard_session_req,
    _dump_json,
    _extract_usage_from_obj,
    _sse_event,
    _usage_dict_has_tokens,
)


def build_openai_bridge_streaming_response(
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
    expose_thinking: bool,
    codex_reinject_trace: Optional[Dict[str, Any]],
    up_res_path: str,
    down_res_path: str,
    session_req_path: Optional[str],
    session_down_res_path: Optional[str],
    session_non_stream_path: Optional[str],
) -> StreamingResponse:
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

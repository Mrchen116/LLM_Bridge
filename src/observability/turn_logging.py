from __future__ import annotations

import glob
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional

from proxy_logging import _dump_json

DOWNSTREAM_FORMAT_ANTHROPIC_MESSAGES = "anthropic_messages"
DOWNSTREAM_FORMAT_OPENAI_CHAT = "openai_chat"
DOWNSTREAM_FORMAT_OPENAI_RESPONSES = "openai_responses"

RAW_BUCKET_OPENAI_CHAT = "openai_chat"
RAW_BUCKET_OPENAI_CODEX = "openai_codex"
RAW_BUCKET_ANTHROPIC = "anthropic"

_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
}


@dataclass(frozen=True)
class TurnLogPaths:
    ts: str
    downstream_format: str
    raw_bucket: str
    raw_req_path: str
    raw_upstream_req_path: str
    raw_headers_path: str
    raw_upstream_res_path: str
    raw_downstream_res_path: str
    session_dir: Optional[str] = None
    session_req_path: Optional[str] = None
    session_downstream_res_path: Optional[str] = None
    session_non_stream_res_path: Optional[str] = None


def resolve_raw_bucket(*, auth_type: str, provider: str) -> str:
    if auth_type == "codex_oauth":
        return RAW_BUCKET_OPENAI_CODEX
    if provider == "anthropic":
        return RAW_BUCKET_ANTHROPIC
    return RAW_BUCKET_OPENAI_CHAT


def _sanitize_headers(headers: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in dict(headers).items():
        key = str(k)
        val = "" if v is None else str(v)
        if key.lower() in _SENSITIVE_HEADERS:
            out[key] = "***"
        else:
            out[key] = val
    return out


def build_turn_log_paths(
    *,
    logs_raw_dir: str,
    logs_session_dir: str,
    raw_bucket: str,
    downstream_format: str,
    session_id: Optional[str],
) -> TurnLogPaths:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
    raw_dir = os.path.join(logs_raw_dir, raw_bucket)
    os.makedirs(raw_dir, exist_ok=True)

    def raw_file(kind: str) -> str:
        return os.path.join(raw_dir, f"{ts}-{kind}-{downstream_format}.json")

    session_dir: Optional[str] = None
    session_req_path: Optional[str] = None
    session_downstream_res_path: Optional[str] = None
    session_non_stream_res_path: Optional[str] = None

    if session_id:
        os.makedirs(logs_session_dir, exist_ok=True)
        existing_dirs = sorted(glob.glob(os.path.join(logs_session_dir, f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join(logs_session_dir, f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        session_req_path = os.path.join(session_dir, f"{ts}-req-{downstream_format}.json")
        session_downstream_res_path = os.path.join(session_dir, f"{ts}-downstream-res-{downstream_format}.json")
        session_non_stream_res_path = os.path.join(session_dir, f"{ts}-non-stream-res-{downstream_format}.json")

    return TurnLogPaths(
        ts=ts,
        downstream_format=downstream_format,
        raw_bucket=raw_bucket,
        raw_req_path=raw_file("req"),
        raw_upstream_req_path=raw_file("upstream-req"),
        raw_headers_path=raw_file("headers"),
        raw_upstream_res_path=raw_file("upstream-res"),
        raw_downstream_res_path=raw_file("downstream-res"),
        session_dir=session_dir,
        session_req_path=session_req_path,
        session_downstream_res_path=session_downstream_res_path,
        session_non_stream_res_path=session_non_stream_res_path,
    )


def log_request_phase(
    paths: TurnLogPaths,
    *,
    request_obj: Dict[str, Any],
    upstream_request_obj: Dict[str, Any],
    client_headers: Mapping[str, Any],
    upstream_headers: Mapping[str, Any],
) -> None:
    _dump_json(paths.raw_req_path, request_obj)
    _dump_json(paths.raw_upstream_req_path, upstream_request_obj)
    _dump_json(
        paths.raw_headers_path,
        {
            "client_headers": _sanitize_headers(client_headers),
            "upstream_headers": _sanitize_headers(upstream_headers),
        },
    )
    if paths.session_req_path:
        _dump_json(paths.session_req_path, request_obj)


def log_response_phase(
    paths: TurnLogPaths,
    *,
    upstream_response_obj: Dict[str, Any],
    downstream_response_obj: Dict[str, Any],
    non_stream_response_obj: Optional[Dict[str, Any]] = None,
) -> None:
    _dump_json(paths.raw_upstream_res_path, upstream_response_obj)
    _dump_json(paths.raw_downstream_res_path, downstream_response_obj)
    status_code = downstream_response_obj.get("status_code") if isinstance(downstream_response_obj, dict) else None
    session_writable = not (isinstance(status_code, int) and status_code >= 400)
    if session_writable and paths.session_downstream_res_path:
        _dump_json(paths.session_downstream_res_path, downstream_response_obj)
    if session_writable and paths.session_non_stream_res_path:
        source_obj = non_stream_response_obj or downstream_response_obj
        _dump_json(
            paths.session_non_stream_res_path,
            build_session_non_stream_openai_chat(source_obj, paths.downstream_format),
        )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _usage_to_openai_chat(usage: Any) -> Dict[str, int]:
    usage_obj = usage if isinstance(usage, dict) else {}
    prompt_tokens = _safe_int(usage_obj.get("prompt_tokens") or usage_obj.get("input_tokens"))
    completion_tokens = _safe_int(usage_obj.get("completion_tokens") or usage_obj.get("output_tokens"))
    total_tokens = _safe_int(usage_obj.get("total_tokens") or (prompt_tokens + completion_tokens))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _new_empty_chat_completion(model: str, resp_id: str = "") -> Dict[str, Any]:
    return {
        "id": resp_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _coerce_openai_chat_non_stream(value: Any, fallback_model: str = "unknown") -> Dict[str, Any]:
    obj = value if isinstance(value, dict) else {}
    if isinstance(obj.get("json"), dict):
        obj = obj.get("json") or {}

    model = str(obj.get("model") or fallback_model)
    out = _new_empty_chat_completion(model=model, resp_id=str(obj.get("id") or ""))
    out["usage"] = _usage_to_openai_chat(obj.get("usage"))

    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        c0 = choices[0]
        msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        role = str(msg.get("role") or "assistant")
        content = msg.get("content")
        if not isinstance(content, str):
            content = ""
        message: Dict[str, Any] = {"role": role, "content": content}
        if isinstance(msg.get("tool_calls"), list):
            message["tool_calls"] = msg.get("tool_calls")
        if isinstance(msg.get("reasoning_content"), str) and msg.get("reasoning_content"):
            message["reasoning_content"] = msg.get("reasoning_content")
        out["choices"][0]["message"] = message
        finish_reason = c0.get("finish_reason")
        out["choices"][0]["finish_reason"] = str(finish_reason or "stop")
        c0_usage = c0.get("usage")
        if isinstance(c0_usage, dict):
            out["usage"] = _usage_to_openai_chat(c0_usage)

    return out


def _map_anthropic_stop_reason(stop_reason: Any) -> str:
    value = str(stop_reason or "")
    if value == "max_tokens":
        return "length"
    if value == "tool_use":
        return "tool_calls"
    return "stop"


def _coerce_anthropic_message_to_openai_chat(value: Any) -> Dict[str, Any]:
    obj = value if isinstance(value, dict) else {}
    model = str(obj.get("model") or "unknown")
    out = _new_empty_chat_completion(model=model, resp_id=str(obj.get("id") or ""))
    out["usage"] = _usage_to_openai_chat(obj.get("usage"))
    out["choices"][0]["finish_reason"] = _map_anthropic_stop_reason(obj.get("stop_reason"))

    blocks = obj.get("content")
    if not isinstance(blocks, list):
        return out

    text_parts: List[str] = []
    thinking_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type == "text":
            text_parts.append(str(block.get("text") or ""))
            continue
        if block_type == "thinking":
            thinking_parts.append(str(block.get("thinking") or ""))
            continue
        if block_type != "tool_use":
            continue

        name = str(block.get("name") or "unknown_tool")
        tool_call_id = str(block.get("id") or f"call_{uuid.uuid4().hex}")
        arguments_obj = block.get("input")
        if isinstance(arguments_obj, str):
            arguments = arguments_obj
        else:
            try:
                arguments = json.dumps(arguments_obj if arguments_obj is not None else {}, ensure_ascii=False)
            except Exception:
                arguments = "{}"
        tool_calls.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )

    message = out["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thinking_parts:
        message["reasoning_content"] = "".join(thinking_parts)
    return out


def _extract_text_from_responses_output(output: Any) -> str:
    if not isinstance(output, list):
        return ""
    text_parts: List[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"output_text", "text"}:
            text_parts.append(str(item.get("text") or ""))
            continue
        if item_type != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type in {"output_text", "text"}:
                text_parts.append(str(part.get("text") or ""))
    return "".join(text_parts)


def _extract_tool_calls_from_responses_output(output: Any) -> List[Dict[str, Any]]:
    if not isinstance(output, list):
        return []
    tool_calls: List[Dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "function_call":
            continue
        name = str(item.get("name") or "unknown_tool")
        tool_call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}")
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            except Exception:
                arguments = "{}"
        tool_calls.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return tool_calls


def _coerce_openai_responses_to_openai_chat(value: Any) -> Dict[str, Any]:
    obj = value if isinstance(value, dict) else {}
    if isinstance(obj.get("json"), dict):
        obj = obj.get("json") or {}

    model = str(obj.get("model") or "unknown")
    out = _new_empty_chat_completion(model=model, resp_id=str(obj.get("id") or ""))
    out["usage"] = _usage_to_openai_chat(obj.get("usage"))

    output = obj.get("output")
    if not isinstance(output, list):
        return out

    text = _extract_text_from_responses_output(output)
    tool_calls = _extract_tool_calls_from_responses_output(output)
    message = out["choices"][0]["message"]
    message["content"] = text
    if tool_calls:
        message["tool_calls"] = tool_calls
        out["choices"][0]["finish_reason"] = "tool_calls"
    return out


def build_session_non_stream_openai_chat(value: Any, downstream_format: str) -> Dict[str, Any]:
    if downstream_format == DOWNSTREAM_FORMAT_ANTHROPIC_MESSAGES:
        return _coerce_anthropic_message_to_openai_chat(value)
    if downstream_format == DOWNSTREAM_FORMAT_OPENAI_RESPONSES:
        return _coerce_openai_responses_to_openai_chat(value)
    return _coerce_openai_chat_non_stream(value)


def build_openai_chat_non_stream_from_sse_chunks(chunks: List[Any], fallback_model: str) -> Dict[str, Any]:
    text_parts: List[str] = []
    usage: Dict[str, Any] = {}
    finish_reason: Optional[str] = None
    resp_id: Optional[str] = None
    model = fallback_model
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    next_tool_idx = 0

    for chunk in chunks:
        if not isinstance(chunk, str):
            continue
        line = chunk.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            data = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        if isinstance(data.get("id"), str):
            resp_id = data.get("id")
        if isinstance(data.get("model"), str):
            model = data.get("model")
        if isinstance(data.get("usage"), dict):
            usage = dict(data.get("usage"))

        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            c0 = choices[0]
            if isinstance(c0.get("usage"), dict):
                usage = dict(c0.get("usage"))
            if c0.get("finish_reason") is not None:
                finish_reason = c0.get("finish_reason")
            delta = c0.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    text_parts.append(content)

                delta_tool_calls = delta.get("tool_calls")
                if isinstance(delta_tool_calls, list):
                    for tc in delta_tool_calls:
                        if not isinstance(tc, dict):
                            continue

                        raw_idx = tc.get("index")
                        if isinstance(raw_idx, int):
                            tool_idx = raw_idx
                        else:
                            tool_idx = next_tool_idx
                        next_tool_idx = max(next_tool_idx, tool_idx + 1)

                        merged = tool_calls_by_index.get(tool_idx)
                        if not isinstance(merged, dict):
                            merged = {"type": "function", "function": {}}

                        if isinstance(tc.get("id"), str):
                            merged["id"] = tc.get("id")
                        if isinstance(tc.get("type"), str):
                            merged["type"] = tc.get("type")

                        fn = tc.get("function")
                        if isinstance(fn, dict):
                            merged_fn = merged.get("function") if isinstance(merged.get("function"), dict) else {}
                            if isinstance(fn.get("name"), str) and fn.get("name"):
                                merged_fn["name"] = fn.get("name")
                            if isinstance(fn.get("arguments"), str):
                                prev_args = merged_fn.get("arguments") if isinstance(merged_fn.get("arguments"), str) else ""
                                merged_fn["arguments"] = f"{prev_args}{fn.get('arguments')}"
                            merged["function"] = merged_fn

                        tool_calls_by_index[tool_idx] = merged

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls_by_index:
        message["tool_calls"] = [tool_calls_by_index[idx] for idx in sorted(tool_calls_by_index.keys())]

    return {
        "id": resp_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def build_openai_responses_non_stream_from_sse_chunks(chunks: List[Any]) -> Dict[str, Any]:
    def iter_sse_data_objects() -> List[Dict[str, Any]]:
        merged = "".join(chunk for chunk in chunks if isinstance(chunk, str))
        if not merged:
            return []
        merged = merged.replace("\r\n", "\n")
        out: List[Dict[str, Any]] = []
        for frame in merged.split("\n\n"):
            if not frame.strip():
                continue
            data_lines: List[str] = []
            for line in frame.split("\n"):
                if not line.startswith("data:"):
                    continue
                data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data_str = "\n".join(data_lines).strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                obj = json.loads(data_str)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    completed: Optional[Dict[str, Any]] = None
    for data in iter_sse_data_objects():
        if str(data.get("type") or "") == "response.completed":
            resp = data.get("response")
            if isinstance(resp, dict):
                completed = resp

    if completed is not None:
        return completed

    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }

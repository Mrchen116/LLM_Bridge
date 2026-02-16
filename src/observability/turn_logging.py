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
    if paths.session_downstream_res_path:
        _dump_json(paths.session_downstream_res_path, downstream_response_obj)
    if paths.session_non_stream_res_path:
        _dump_json(paths.session_non_stream_res_path, non_stream_response_obj or downstream_response_obj)


def build_openai_chat_non_stream_from_sse_chunks(chunks: List[Any], fallback_model: str) -> Dict[str, Any]:
    text_parts: List[str] = []
    usage: Dict[str, Any] = {}
    finish_reason: Optional[str] = None
    resp_id: Optional[str] = None
    model = fallback_model

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

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

    return {
        "id": resp_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(text_parts)},
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
    completed: Optional[Dict[str, Any]] = None
    for chunk in chunks:
        if not isinstance(chunk, str):
            continue
        for line in chunk.splitlines():
            line = line.strip()
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

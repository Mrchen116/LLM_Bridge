from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import tiktoken

from proxy_converters import calculate_token_count

TOKEN_FORMAT_ANTHROPIC = "anthropic_messages"
TOKEN_FORMAT_OPENAI_CHAT = "openai_chat"
TOKEN_FORMAT_OPENAI_RESPONSES = "openai_responses"
TOKEN_FORMAT_UNKNOWN = "unknown"

KNOWN_TOKEN_FORMATS = {
    TOKEN_FORMAT_ANTHROPIC,
    TOKEN_FORMAT_OPENAI_CHAT,
    TOKEN_FORMAT_OPENAI_RESPONSES,
}

_FORMAT_FROM_FILENAME_RE = re.compile(
    r"-(anthropic_messages|openai_chat|openai_responses)\.json$"
)

_ENC = None


def _get_encoder():
    global _ENC
    if _ENC is None:
        _ENC = tiktoken.get_encoding("cl100k_base")
    return _ENC


def _count_text_tokens(text: Any) -> int:
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    return len(_get_encoder().encode(text))


def _count_json_tokens(value: Any) -> int:
    if value is None:
        return 0
    try:
        dumped = json.dumps(value, ensure_ascii=False)
    except Exception:
        dumped = str(value)
    return _count_text_tokens(dumped)


def _count_message_content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return _count_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += _count_text_tokens(part)
                continue
            if not isinstance(part, dict):
                total += _count_text_tokens(part)
                continue
            p_type = str(part.get("type") or "")
            if p_type in {"text", "input_text", "output_text"}:
                total += _count_text_tokens(part.get("text"))
            elif p_type == "thinking":
                total += _count_text_tokens(part.get("thinking"))
            elif p_type == "tool_use":
                total += _count_text_tokens(part.get("name"))
                total += _count_json_tokens(part.get("input"))
            elif p_type == "tool_result":
                total += _count_text_tokens(part.get("tool_use_id"))
                total += _count_json_tokens(part.get("content"))
            elif p_type == "input_image":
                total += _count_text_tokens(part.get("image_url"))
            else:
                total += _count_json_tokens(part)
        return total
    if isinstance(content, dict):
        return _count_json_tokens(content)
    return _count_text_tokens(content)


def _count_tools_tokens(tools: Any) -> int:
    if not isinstance(tools, list):
        return 0
    total = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        total += _count_text_tokens(tool.get("name"))
        total += _count_text_tokens(tool.get("description"))
        total += _count_json_tokens(tool.get("input_schema"))
        fn = tool.get("function")
        if isinstance(fn, dict):
            total += _count_text_tokens(fn.get("name"))
            total += _count_text_tokens(fn.get("description"))
            total += _count_json_tokens(fn.get("parameters"))
    return total


def _count_openai_chat_input_tokens(body: Dict[str, Any]) -> int:
    total = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                total += _count_text_tokens(msg)
                continue
            total += _count_message_content_tokens(msg.get("content"))
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        total += _count_text_tokens(fn.get("name"))
                        total += _count_text_tokens(fn.get("arguments"))
            total += _count_text_tokens(msg.get("tool_call_id"))
    total += _count_tools_tokens(body.get("tools"))
    return total


def _count_openai_responses_input_tokens(body: Dict[str, Any]) -> int:
    total = 0
    total += _count_text_tokens(body.get("instructions"))

    input_value = body.get("input")
    if isinstance(input_value, str):
        total += _count_text_tokens(input_value)
    elif isinstance(input_value, list):
        for item in input_value:
            if isinstance(item, str):
                total += _count_text_tokens(item)
                continue
            if not isinstance(item, dict):
                total += _count_text_tokens(item)
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"function_call", "tool_call"}:
                total += _count_text_tokens(item.get("name"))
                total += _count_text_tokens(item.get("arguments"))
                continue
            if item_type == "function_call_output":
                total += _count_text_tokens(item.get("call_id"))
                total += _count_text_tokens(item.get("output"))
                continue
            total += _count_message_content_tokens(item.get("content"))
    elif input_value is not None:
        total += _count_text_tokens(input_value)

    total += _count_tools_tokens(body.get("tools"))
    return total


def normalize_token_format(raw: Any, default_format: str) -> str:
    value = str(raw or "").strip().lower()
    if value in KNOWN_TOKEN_FORMATS:
        return value
    return default_format


def detect_token_format_from_request(body: Dict[str, Any], default_format: str = TOKEN_FORMAT_ANTHROPIC) -> str:
    explicit = body.get("format")
    if explicit is not None:
        return normalize_token_format(explicit, default_format)

    has_input = "input" in body
    has_messages = "messages" in body
    if has_input and not has_messages:
        return TOKEN_FORMAT_OPENAI_RESPONSES
    if has_messages:
        # /v1/messages/count_tokens 历史默认按 anthropic 语义解释。
        return default_format
    return default_format


def count_input_tokens_for_request(
    body: Dict[str, Any],
    *,
    default_format: str = TOKEN_FORMAT_ANTHROPIC,
) -> Tuple[str, int]:
    fmt = detect_token_format_from_request(body, default_format=default_format)
    if fmt == TOKEN_FORMAT_ANTHROPIC:
        messages = body.get("messages", [])
        system = body.get("system")
        tools = body.get("tools")
        return fmt, int(calculate_token_count(messages, system, tools))
    if fmt == TOKEN_FORMAT_OPENAI_CHAT:
        return fmt, int(_count_openai_chat_input_tokens(body))
    if fmt == TOKEN_FORMAT_OPENAI_RESPONSES:
        return fmt, int(_count_openai_responses_input_tokens(body))
    return fmt, 0


def infer_token_format_from_path(path: str) -> Optional[str]:
    m = _FORMAT_FROM_FILENAME_RE.search(path or "")
    if not m:
        return None
    return m.group(1)


def _usage_pair_for_format(usage: Any, fmt: str) -> Tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    try:
        if fmt == TOKEN_FORMAT_OPENAI_CHAT:
            return (
                int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
                int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            )
        if fmt in {TOKEN_FORMAT_ANTHROPIC, TOKEN_FORMAT_OPENAI_RESPONSES}:
            return (
                int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
            )
    except Exception:
        return 0, 0
    return 0, 0


def _usage_pair_unknown(usage: Any) -> Tuple[int, int, str]:
    if not isinstance(usage, dict):
        return 0, 0, TOKEN_FORMAT_UNKNOWN
    has_chat = ("prompt_tokens" in usage) or ("completion_tokens" in usage)
    has_io = ("input_tokens" in usage) or ("output_tokens" in usage)
    try:
        if has_chat and not has_io:
            return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0), TOKEN_FORMAT_OPENAI_CHAT
        if has_io:
            return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), TOKEN_FORMAT_ANTHROPIC
    except Exception:
        return 0, 0, TOKEN_FORMAT_UNKNOWN
    return 0, 0, TOKEN_FORMAT_UNKNOWN


def _iter_event_usages(events: Any) -> Iterable[Any]:
    if not isinstance(events, list):
        return []
    out: List[Any] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data")
        if isinstance(data, dict):
            out.append(data.get("usage"))
    return out


def _iter_chunk_usages(chunks: Any) -> Iterable[Any]:
    if not isinstance(chunks, list):
        return []
    out: List[Any] = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            out.append(chunk.get("usage"))
            if isinstance(chunk.get("json"), dict):
                out.append(chunk.get("json", {}).get("usage"))
            continue
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
        if isinstance(data, dict):
            out.append(data.get("usage"))
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                out.append((choices[0] or {}).get("usage"))
    return out


def collect_usage_tokens_for_stats(obj: Any, path: str = "") -> Tuple[int, int, str]:
    fmt = infer_token_format_from_path(path)

    if isinstance(obj, dict):
        direct_usage = None
        if isinstance(obj.get("usage"), dict):
            direct_usage = obj.get("usage")
        elif isinstance(obj.get("json"), dict) and isinstance(obj.get("json", {}).get("usage"), dict):
            direct_usage = obj.get("json", {}).get("usage")
        if direct_usage is not None:
            if fmt is not None:
                in_tok, out_tok = _usage_pair_for_format(direct_usage, fmt)
                return in_tok, out_tok, fmt
            in_tok, out_tok, inferred = _usage_pair_unknown(direct_usage)
            return in_tok, out_tok, inferred

        # Anthropic-style events: sum all usage frames.
        event_total_in = 0
        event_total_out = 0
        saw_event_usage = False
        for usage in _iter_event_usages(obj.get("events")):
            if fmt is not None:
                in_tok, out_tok = _usage_pair_for_format(usage, fmt)
                saw_event_usage = saw_event_usage or bool(in_tok or out_tok)
                event_total_in += in_tok
                event_total_out += out_tok
            else:
                in_tok, out_tok, inferred = _usage_pair_unknown(usage)
                saw_event_usage = saw_event_usage or bool(in_tok or out_tok)
                event_total_in += in_tok
                event_total_out += out_tok
                if fmt is None and inferred != TOKEN_FORMAT_UNKNOWN:
                    fmt = inferred
        if saw_event_usage:
            return event_total_in, event_total_out, (fmt or TOKEN_FORMAT_UNKNOWN)

        # Chunk-style captures: keep last usage frame.
        last_in = 0
        last_out = 0
        saw_chunk_usage = False
        for usage in _iter_chunk_usages(obj.get("chunks")):
            if fmt is not None:
                in_tok, out_tok = _usage_pair_for_format(usage, fmt)
                if in_tok or out_tok:
                    last_in, last_out = in_tok, out_tok
                    saw_chunk_usage = True
            else:
                in_tok, out_tok, inferred = _usage_pair_unknown(usage)
                if in_tok or out_tok:
                    last_in, last_out = in_tok, out_tok
                    saw_chunk_usage = True
                if fmt is None and inferred != TOKEN_FORMAT_UNKNOWN:
                    fmt = inferred
        if saw_chunk_usage:
            return last_in, last_out, (fmt or TOKEN_FORMAT_UNKNOWN)

    return 0, 0, (fmt or TOKEN_FORMAT_UNKNOWN)

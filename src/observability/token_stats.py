from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import tiktoken

from proxy_converters import calculate_token_count, _extract_text_from_blocks, _message_content_to_text

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

# Matches data URIs containing base64-encoded image data (PNG, JPEG, GIF, WebP, SVG, etc.)
# Minimum 100 chars of base64 payload to avoid false positives on tiny inline images.
_BASE64_IMAGE_RE = re.compile(
    r'data:image/[^;\'"\s]{1,20};base64,[A-Za-z0-9+/=]{100,}',
    re.ASCII,
)


def _get_encoder():
    global _ENC
    if _ENC is None:
        _ENC = tiktoken.get_encoding("cl100k_base")
    return _ENC


_IMAGE_BLOCK_TYPES = {"image", "input_image", "image_url"}


def _content_has_images(content: Any) -> bool:
    """Recursively check if content contains any image blocks (Anthropic/OpenAI style)."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _IMAGE_BLOCK_TYPES:
            return True
        # Recurse into nested content (e.g. tool_result.content)
        if _content_has_images(block.get("content")):
            return True
    return False


def _strip_base64_images(text: str) -> Tuple[str, bool]:
    """Remove base64 image data URIs from text. Returns (cleaned_text, had_images)."""
    if 'base64,' not in text:
        return text, False
    cleaned = _BASE64_IMAGE_RE.sub('', text)
    return cleaned, cleaned != text


def _count_text_tokens(text: Any) -> int:
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    return len(_get_encoder().encode(text))


def _count_text_tokens_no_images(text: Any) -> Tuple[int, bool]:
    """Count tokens after stripping base64 image data. Returns (count, had_images)."""
    if text is None:
        return 0, False
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0, False
    cleaned, had_images = _strip_base64_images(text)
    return len(_get_encoder().encode(cleaned)), had_images


def _count_json_tokens_no_images(value: Any) -> Tuple[int, bool]:
    """Serialize to JSON, strip base64 images, count tokens."""
    if value is None:
        return 0, False
    try:
        dumped = json.dumps(value, ensure_ascii=False)
    except Exception:
        dumped = str(value)
    return _count_text_tokens_no_images(dumped)


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


def _build_tool_id_to_name_map(messages: Any) -> Dict[str, str]:
    """Scan messages to map tool call IDs → tool names (supports Anthropic and OpenAI formats)."""
    id_to_name: Dict[str, str] = {}
    if not isinstance(messages, list):
        return id_to_name
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Anthropic: assistant messages with tool_use content blocks
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    tool_name = block.get("name")
                    if tool_id and tool_name:
                        id_to_name[str(tool_id)] = str(tool_name)
        # OpenAI chat: assistant messages with tool_calls array
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                fn = tc.get("function")
                if tc_id and isinstance(fn, dict) and fn.get("name"):
                    id_to_name[str(tc_id)] = str(fn["name"])
    return id_to_name


def _breakdown_anthropic(body: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "system_prompt": 0,
        "tool_definitions": 0,
        "user_messages": 0,
        "tool_calls": 0,
        "tool_results_by_tool": {},
        "assistant_text": 0,
        "assistant_reasoning": 0,
        "has_encrypted_reasoning": False,
        "has_uncountable_image_content": False,
    }

    # System prompt — use _extract_text_from_blocks (skips image blocks naturally)
    result["system_prompt"] = _count_text_tokens(_extract_text_from_blocks(body.get("system")))
    result["tool_definitions"] = _count_tools_tokens(body.get("tools"))

    messages = body.get("messages") or []
    id_to_name = _build_tool_id_to_name_map(messages)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                result["user_messages"] += _count_text_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "")
                    if btype in _IMAGE_BLOCK_TYPES:
                        result["has_uncountable_image_content"] = True
                    elif btype == "text":
                        result["user_messages"] += _count_text_tokens(block.get("text"))
                    elif btype == "tool_result":
                        tool_id = str(block.get("tool_use_id") or "")
                        tool_name = id_to_name.get(tool_id, "(unknown)")
                        inner = block.get("content")
                        if _content_has_images(inner):
                            result["has_uncountable_image_content"] = True
                        # _extract_text_from_blocks skips image blocks, counts only text
                        toks = _count_text_tokens(_extract_text_from_blocks(inner))
                        by_tool = result["tool_results_by_tool"]
                        by_tool[tool_name] = by_tool.get(tool_name, 0) + toks

        elif role == "assistant":
            if isinstance(content, str):
                result["assistant_text"] += _count_text_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "")
                    if btype == "text":
                        result["assistant_text"] += _count_text_tokens(block.get("text"))
                    elif btype == "thinking":
                        result["assistant_reasoning"] += _count_text_tokens(block.get("thinking"))
                    elif btype == "redacted_thinking":
                        result["has_encrypted_reasoning"] = True
                    elif btype == "tool_use":
                        result["tool_calls"] += _count_text_tokens(block.get("name"))
                        result["tool_calls"] += _count_json_tokens(block.get("input"))

    return result


def _breakdown_openai_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "system_prompt": 0,
        "tool_definitions": 0,
        "user_messages": 0,
        "tool_calls": 0,
        "tool_results_by_tool": {},
        "assistant_text": 0,
        "assistant_reasoning": 0,
        "has_encrypted_reasoning": False,
        "has_uncountable_image_content": False,
    }
    result["tool_definitions"] = _count_tools_tokens(body.get("tools"))

    messages = body.get("messages") or []
    id_to_name = _build_tool_id_to_name_map(messages)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")

        if role == "system":
            result["system_prompt"] += _count_text_tokens(_message_content_to_text(content))
        elif role == "user":
            if _content_has_images(content):
                result["has_uncountable_image_content"] = True
            result["user_messages"] += _count_text_tokens(_message_content_to_text(content))
        elif role == "assistant":
            result["assistant_text"] += _count_text_tokens(_message_content_to_text(content))
            rc = msg.get("reasoning_content")
            if rc:
                result["assistant_reasoning"] += _count_text_tokens(_message_content_to_text(rc))
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    if isinstance(fn, dict):
                        result["tool_calls"] += _count_text_tokens(fn.get("name"))
                        result["tool_calls"] += _count_text_tokens(fn.get("arguments"))
        elif role == "tool":
            tool_call_id = str(msg.get("tool_call_id") or "")
            tool_name = id_to_name.get(tool_call_id, "(unknown)")
            # tool content is a string; strip data-URI images
            toks, had_img = _count_text_tokens_no_images(content if isinstance(content, str) else json.dumps(content or "", ensure_ascii=False))
            if had_img:
                result["has_uncountable_image_content"] = True
            by_tool = result["tool_results_by_tool"]
            by_tool[tool_name] = by_tool.get(tool_name, 0) + toks

    return result


def _breakdown_openai_responses(body: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "system_prompt": 0,
        "tool_definitions": 0,
        "user_messages": 0,
        "tool_calls": 0,
        "tool_results_by_tool": {},
        "assistant_text": 0,
        "assistant_reasoning": 0,
        "has_encrypted_reasoning": False,
        "has_uncountable_image_content": False,
    }

    instructions = body.get("instructions")
    if instructions:
        result["system_prompt"] = _count_text_tokens(str(instructions))
    result["tool_definitions"] = _count_tools_tokens(body.get("tools"))

    input_value = body.get("input")
    if isinstance(input_value, str):
        result["user_messages"] = _count_text_tokens(input_value)
        return result
    if not isinstance(input_value, list):
        return result

    # First pass: build call_id → tool_name from function_call items
    id_to_name: Dict[str, str] = {}
    for item in input_value:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype in {"function_call", "tool_call"}:
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            if call_id and name:
                id_to_name[call_id] = name

    for item in input_value:
        if not isinstance(item, dict):
            result["user_messages"] += _count_text_tokens(item)
            continue
        itype = str(item.get("type") or "")
        role = str(item.get("role") or "")

        if itype in {"function_call", "tool_call"}:
            result["tool_calls"] += _count_text_tokens(item.get("name"))
            result["tool_calls"] += _count_text_tokens(item.get("arguments"))
        elif itype == "function_call_output":
            call_id = str(item.get("call_id") or "")
            tool_name = id_to_name.get(call_id, "(unknown)")
            output = item.get("output")
            if isinstance(output, str):
                toks, had_img = _count_text_tokens_no_images(output)
            else:
                toks, had_img = _count_json_tokens_no_images(output)
            if had_img:
                result["has_uncountable_image_content"] = True
            by_tool = result["tool_results_by_tool"]
            by_tool[tool_name] = by_tool.get(tool_name, 0) + toks
        elif itype == "reasoning":
            # Encrypted reasoning (e.g. Codex) – can't count tokens
            if item.get("encrypted_content"):
                result["has_encrypted_reasoning"] = True
            else:
                result["assistant_reasoning"] += _count_message_content_tokens(item.get("content"))
        elif itype == "message":
            content = item.get("content")
            if _content_has_images(content):
                result["has_uncountable_image_content"] = True
            text = _message_content_to_text(content)
            if role in {"developer", "system"}:
                result["system_prompt"] += _count_text_tokens(text)
            elif role == "assistant":
                result["assistant_text"] += _count_text_tokens(text)
            else:
                result["user_messages"] += _count_text_tokens(text)
        elif role in {"developer", "system"} or itype in {"developer", "system"}:
            result["system_prompt"] += _count_text_tokens(_message_content_to_text(item.get("content")))
        elif role in {"user", "assistant"} or itype in {"user", "assistant"}:
            key = "assistant_text" if (role == "assistant" or itype == "assistant") else "user_messages"
            result[key] += _count_text_tokens(_message_content_to_text(item.get("content")))
        else:
            result["user_messages"] += _count_text_tokens(_message_content_to_text(item.get("content")))

    return result


def compute_token_breakdown(
    req_obj: Dict[str, Any],
    *,
    total_input_tokens_from_api: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Compute a per-category token breakdown for a request object.
    The breakdown is estimated using tiktoken. The overall total may come from API usage.
    """
    fmt = detect_token_format_from_request(req_obj)
    if fmt == TOKEN_FORMAT_ANTHROPIC:
        raw = _breakdown_anthropic(req_obj)
    elif fmt == TOKEN_FORMAT_OPENAI_CHAT:
        raw = _breakdown_openai_chat(req_obj)
    elif fmt == TOKEN_FORMAT_OPENAI_RESPONSES:
        raw = _breakdown_openai_responses(req_obj)
    else:
        raw = {
            "system_prompt": 0,
            "tool_definitions": 0,
            "user_messages": 0,
            "tool_calls": 0,
            "tool_results_by_tool": {},
            "assistant_text": 0,
            "assistant_reasoning": 0,
            "has_encrypted_reasoning": False,
        }

    has_encrypted = bool(raw.pop("has_encrypted_reasoning", False))
    has_uncountable_images = bool(raw.pop("has_uncountable_image_content", False))
    by_tool_dict: Dict[str, int] = raw.pop("tool_results_by_tool", {})
    tool_results_total = sum(by_tool_dict.values())
    by_tool_list = sorted(
        [{"tool_name": k, "tokens": v} for k, v in by_tool_dict.items()],
        key=lambda x: -x["tokens"],
    )

    breakdown = {
        "system_prompt": raw.get("system_prompt", 0),
        "tool_definitions": raw.get("tool_definitions", 0),
        "user_messages": raw.get("user_messages", 0),
        "tool_calls": raw.get("tool_calls", 0),
        "tool_results": {
            "total": tool_results_total,
            "by_tool": by_tool_list,
        },
        "assistant_text": raw.get("assistant_text", 0),
        "assistant_reasoning": raw.get("assistant_reasoning", 0),
    }

    estimated_total = (
        breakdown["system_prompt"]
        + breakdown["tool_definitions"]
        + breakdown["user_messages"]
        + breakdown["tool_calls"]
        + tool_results_total
        + breakdown["assistant_text"]
        + breakdown["assistant_reasoning"]
    )

    return {
        "total_input_tokens": total_input_tokens_from_api if total_input_tokens_from_api is not None else estimated_total,
        "total_from_api": total_input_tokens_from_api is not None,
        "estimated_total": estimated_total,
        "breakdown": breakdown,
        "has_encrypted_reasoning": has_encrypted,
        "has_uncountable_image_content": has_uncountable_images,
    }


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

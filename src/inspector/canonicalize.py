from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _chat_content_to_responses_input_parts,
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai_tools,
)
from src.reasoning.reinject import (
    _split_trailing_user_suffix_oai_messages,
    _split_trailing_user_suffix_responses_input,
)

KNOWN_FORMATS = {
    "anthropic_messages",
    "openai_chat",
    "openai_responses",
}

_SYSTEM_REMINDER_RE = re.compile(
    r"<system-reminder>[\s\S]*?</system-reminder>",
    flags=re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def context_fingerprint(context_key: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(context_key).encode("utf-8")).hexdigest()


_CCH_RE = re.compile(r"cch=[a-f0-9]+;")


def _normalize_space(value: str) -> str:
    return " ".join(value.split()).strip()


def strip_system_reminder(text: str) -> str:
    return _normalize_space(_SYSTEM_REMINDER_RE.sub(" ", text or ""))


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            p_type = str(part.get("type") or "")
            if p_type in {"text", "output_text", "input_text"}:
                parts.append(str(part.get("text") or ""))
                continue
            if "text" in part and part.get("text") is not None:
                parts.append(str(part.get("text") or ""))
                continue
            # anthropic tool_result style: {type: tool_result, content: [...]}
            if part.get("content") is not None:
                nested = _extract_text_from_content(part.get("content"))
                if nested:
                    parts.append(nested)
                    continue
        return "".join(parts)
    if isinstance(content, dict):
        if content.get("text") is not None:
            return str(content.get("text") or "")
        if content.get("content") is not None:
            return _extract_text_from_content(content.get("content"))
    return ""


def _extract_text_from_responses_input_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            p_type = str(part.get("type") or "")
            if p_type in {"input_text", "output_text", "text"}:
                parts.append(str(part.get("text") or ""))
                continue
            if "text" in part and part.get("text") is not None:
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


def agent_prefix_from_context_key(context_key: Dict[str, Any], prefix_len: int = 180) -> str:
    input_items = context_key.get("input")
    if not isinstance(input_items, list):
        return ""
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "user":
            continue
        text = _extract_text_from_responses_input_content(item.get("content"))
        cleaned = strip_system_reminder(text)
        if cleaned:
            return cleaned[:prefix_len]
    return ""


def infer_downstream_format(req_obj: Dict[str, Any], file_format: Optional[str]) -> str:
    if file_format in KNOWN_FORMATS:
        return str(file_format)

    if isinstance(req_obj.get("input"), list):
        return "openai_responses"

    messages = req_obj.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and str(part.get("type") or "") in {
                        "text",
                        "tool_use",
                        "tool_result",
                        "thinking",
                    }:
                        return "anthropic_messages"
        if req_obj.get("system") is not None:
            return "anthropic_messages"
        return "openai_chat"

    return "openai_chat"


def _build_chat_body_from_anthropic(req_obj: Dict[str, Any]) -> Dict[str, Any]:
    tools = anthropic_tools_to_openai_tools(req_obj.get("tools"))
    tool_choice = anthropic_tool_choice_to_openai(req_obj.get("tool_choice"))
    chat_body: Dict[str, Any] = {
        "model": str(req_obj.get("model") or "unknown"),
        "messages": anthropic_messages_to_openai(req_obj.get("messages") or [], req_obj.get("system")),
        "stream": bool(req_obj.get("stream", False)),
    }
    if tools:
        chat_body["tools"] = tools
    if tool_choice is not None:
        chat_body["tool_choice"] = tool_choice
    if req_obj.get("reasoning") is not None:
        chat_body["reasoning"] = req_obj.get("reasoning")
    if req_obj.get("reasoning_effort") is not None:
        chat_body["reasoning_effort"] = req_obj.get("reasoning_effort")
    if req_obj.get("include") is not None:
        chat_body["include"] = req_obj.get("include")
    if req_obj.get("instructions") is not None:
        chat_body["instructions"] = req_obj.get("instructions")
    return chat_body


def _build_chat_body_from_openai_chat(req_obj: Dict[str, Any]) -> Dict[str, Any]:
    chat_body: Dict[str, Any] = {
        "model": str(req_obj.get("model") or "unknown"),
        "messages": req_obj.get("messages") if isinstance(req_obj.get("messages"), list) else [],
        "stream": bool(req_obj.get("stream", False)),
    }
    for key in ("tools", "tool_choice", "reasoning", "reasoning_effort", "include", "instructions"):
        if req_obj.get(key) is not None:
            chat_body[key] = req_obj.get(key)
    return chat_body


def _responses_context_key_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "instructions": payload.get("instructions"),
        "input": payload.get("input") or [],
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
        "reasoning": payload.get("reasoning"),
        "include": payload.get("include"),
    }


def _normalize_billing_header_in_content(content: Any) -> Any:
    """Replace changing cch values in billing header with a stable placeholder."""
    if isinstance(content, str):
        return _CCH_RE.sub("cch=_;", content)
    if isinstance(content, list):
        out: List[Any] = []
        for part in content:
            if isinstance(part, dict) and "text" in part:
                text = str(part.get("text") or "")
                normalized = _CCH_RE.sub("cch=_;", text)
                if normalized != text:
                    out.append({**part, "text": normalized})
                else:
                    out.append(part)
            else:
                out.append(part)
        return out
    return content


def _normalize_billing_header_in_input_items(
    input_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize system messages that contain x-anthropic-billing-header."""
    out: List[Dict[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        if str(item.get("role") or "") != "system":
            out.append(item)
            continue
        content = item.get("content")
        normalized = _normalize_billing_header_in_content(content)
        if normalized is not content:
            out.append({**item, "content": normalized})
        else:
            out.append(item)
    return out


def canonical_context_from_req(req_obj: Dict[str, Any], downstream_format: str) -> Tuple[Dict[str, Any], str]:
    if downstream_format == "openai_responses":
        payload = {
            "instructions": req_obj.get("instructions"),
            "input": req_obj.get("input") if isinstance(req_obj.get("input"), list) else [],
            "tools": req_obj.get("tools"),
            "tool_choice": req_obj.get("tool_choice"),
            "reasoning": req_obj.get("reasoning"),
            "include": req_obj.get("include"),
        }
        prefix_input, _ = _split_trailing_user_suffix_responses_input(payload["input"])
        payload["input"] = _normalize_billing_header_in_input_items(prefix_input)
        context_key = _responses_context_key_from_payload(payload)
        return context_key, str(req_obj.get("model") or "unknown")

    if downstream_format == "anthropic_messages":
        chat_body = _build_chat_body_from_anthropic(req_obj)
    else:
        chat_body = _build_chat_body_from_openai_chat(req_obj)

    messages = chat_body.get("messages") if isinstance(chat_body.get("messages"), list) else []
    prefix_messages, _ = _split_trailing_user_suffix_oai_messages(messages)
    prefix_chat_body = dict(chat_body)
    prefix_chat_body["messages"] = prefix_messages
    model = str(chat_body.get("model") or "unknown")
    responses_payload = _build_codex_responses_payload_from_chat(prefix_chat_body, model)
    context_key = _responses_context_key_from_payload(responses_payload)
    raw_input = context_key.get("input") if isinstance(context_key.get("input"), list) else []
    context_key["input"] = _normalize_billing_header_in_input_items(raw_input)
    return context_key, model


def first_user_text_for_label(req_obj: Dict[str, Any], downstream_format: str) -> str:
    if downstream_format == "openai_responses":
        input_items = req_obj.get("input") if isinstance(req_obj.get("input"), list) else []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "") != "user":
                continue
            content = item.get("content")
            text = _extract_text_from_content(content)
            if text:
                cleaned = strip_system_reminder(text)
                if cleaned:
                    return cleaned
            if isinstance(content, list):
                joined = "".join(_extract_text_from_content(part) for part in content)
                if joined:
                    cleaned = strip_system_reminder(joined)
                    if cleaned:
                        return cleaned
        return ""

    messages = req_obj.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "user":
            continue
        text = _extract_text_from_content(msg.get("content"))
        if text:
            cleaned = strip_system_reminder(text)
            if cleaned:
                return cleaned
    return ""


def extract_last_user_summary(req_obj: Dict[str, Any], downstream_format: str) -> str:
    if downstream_format == "openai_responses":
        input_items = req_obj.get("input") if isinstance(req_obj.get("input"), list) else []
        for item in reversed(input_items):
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "") != "user":
                continue
            content = item.get("content")
            if isinstance(content, list):
                parts = _chat_content_to_responses_input_parts(content)
                text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
            else:
                text = _extract_text_from_content(content)
            if text:
                cleaned = strip_system_reminder(text)
                if cleaned:
                    return cleaned
        return ""

    messages = req_obj.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "user":
            continue
        text = _extract_text_from_content(msg.get("content"))
        if text:
            cleaned = strip_system_reminder(text)
            if cleaned:
                return cleaned
    return ""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _codex_responses_to_chat_completion,
    _extract_codex_output_tool_uses,
)
from src.state.memory_store import InMemoryReasoningStore
from src.state.interfaces import ReasoningStore


_REASONING_STORE: ReasoningStore = InMemoryReasoningStore()


def get_reasoning_store() -> ReasoningStore:
    return _REASONING_STORE



def _split_trailing_user_suffix_oai_messages(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(messages, list) or not messages:
        return [], []
    idx = len(messages)
    while idx > 0 and isinstance(messages[idx - 1], dict):
        role = str(messages[idx - 1].get("role") or "")
        if role not in {"user", "tool"}:
            break
        idx -= 1
    return messages[:idx], messages[idx:]


def _build_codex_chat_body_with_messages(base_body: Dict[str, Any], messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    cloned = dict(base_body)
    cloned["messages"] = messages
    return cloned


def _split_trailing_user_suffix_responses_input(
    input_items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not isinstance(input_items, list) or not input_items:
        return [], []
    idx = len(input_items)
    while idx > 0 and isinstance(input_items[idx - 1], dict):
        role = str(input_items[idx - 1].get("role") or "")
        item_type = str(input_items[idx - 1].get("type") or "")
        if role not in {"user"} and item_type not in {"function_call_output"}:
            break
        idx -= 1
    return input_items[:idx], input_items[idx:]


def _codex_context_fingerprint(payload: Dict[str, Any]) -> str:
    normalized_input: List[Any] = []
    raw_input = payload.get("input")
    if isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                normalized_input.append(item)
                continue
            normalized_item: Dict[str, Any] = copy.deepcopy(item)
            item_type = str(normalized_item.get("type") or "")
            if item_type in {"function_call", "function_call_output"}:
                field = "arguments" if item_type == "function_call" else "output"
                value = normalized_item.get(field)
                if isinstance(value, str):
                    stripped = value.strip()
                    if stripped:
                        try:
                            parsed = json.loads(stripped)
                            normalized_item[field] = json.dumps(
                                parsed,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        except Exception:
                            pass
            normalized_input.append(normalized_item)

    key_obj = {
        "instructions": payload.get("instructions"),
        "input": normalized_input if normalized_input else (payload.get("input") or []),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
        "reasoning": payload.get("reasoning"),
        "include": payload.get("include"),
    }
    canonical = json.dumps(key_obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_codex_reasoning_encrypted_items(resp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = resp_json.get("output")
    if not isinstance(out, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in out:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "reasoning":
            continue
        enc = item.get("encrypted_content")
        if isinstance(enc, str) and enc:
            summary = item.get("summary")
            if not isinstance(summary, list):
                summary = []
            entry: Dict[str, Any] = {
                "type": "reasoning",
                "encrypted_content": enc,
                "summary": copy.deepcopy(summary),
            }
            rid = item.get("id")
            if isinstance(rid, str) and rid:
                entry["id"] = rid
            items.append(entry)
    return items


def _build_assistant_message_from_codex_response(resp_json: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    chat_obj = _codex_responses_to_chat_completion(resp_json, str(resp_json.get("model") or ""))
    text = ""
    choices = chat_obj.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict):
            text = str(msg.get("content") or "")

    tool_uses = _extract_codex_output_tool_uses(resp_json)
    if not text and not tool_uses:
        return None

    assistant: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_uses:
        assistant["tool_calls"] = [
            {
                "id": str(t.get("id") or f"toolu_{uuid.uuid4().hex}"),
                "type": "function",
                "function": {
                    "name": str(t.get("name") or "unknown"),
                    "arguments": json.dumps(t.get("input") or {}, ensure_ascii=False),
                },
            }
            for t in tool_uses
        ]
    return assistant


def _maybe_reinject_codex_reasoning(
    *,
    session_id: Optional[str],
    provider: str,
    model: str,
    codex_chat_body: Dict[str, Any],
    codex_payload: Dict[str, Any],
    model_suffix_effort: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if not session_id:
        return codex_payload, None
    messages = codex_chat_body.get("messages")
    if not isinstance(messages, list):
        return codex_payload, None

    prefix_msgs, suffix_user_msgs = _split_trailing_user_suffix_oai_messages(messages)
    prefix_body = _build_codex_chat_body_with_messages(codex_chat_body, prefix_msgs)
    prefix_payload = _build_codex_responses_payload_from_chat(
        prefix_body, model, model_suffix_effort=model_suffix_effort
    )
    fp = _codex_context_fingerprint(prefix_payload)

    key = (session_id, provider, model)
    decorated_prefix_input = _REASONING_STORE.get_decorated_prefix(key, fp)

    payload = dict(codex_payload)
    if decorated_prefix_input is not None:
        suffix_input_payload = _build_codex_responses_payload_from_chat(
            _build_codex_chat_body_with_messages(codex_chat_body, suffix_user_msgs),
            model,
            model_suffix_effort=model_suffix_effort,
        )
        suffix_input = suffix_input_payload.get("input") if isinstance(suffix_input_payload.get("input"), list) else []
        payload["input"] = copy.deepcopy(decorated_prefix_input) + list(suffix_input)

    trace = {
        "session_id": session_id,
        "provider": provider,
        "model": model,
        "codex_chat_body": copy.deepcopy(codex_chat_body),
        "sent_input": copy.deepcopy(payload.get("input") if isinstance(payload.get("input"), list) else []),
        "model_suffix_effort": model_suffix_effort,
    }
    return payload, trace


def _update_codex_reasoning_reinject_cache(trace: Optional[Dict[str, Any]], resp_json: Dict[str, Any]) -> None:
    if not trace:
        return
    session_id = trace.get("session_id")
    provider = trace.get("provider")
    model = trace.get("model")
    codex_chat_body = trace.get("codex_chat_body")
    sent_input = trace.get("sent_input")
    if not (isinstance(session_id, str) and session_id and isinstance(provider, str) and isinstance(model, str)):
        return
    if not isinstance(codex_chat_body, dict) or not isinstance(sent_input, list):
        return

    raw_ms = trace.get("model_suffix_effort")
    model_suffix_effort: Optional[str] = raw_ms if isinstance(raw_ms, str) else None

    assistant_msg = _build_assistant_message_from_codex_response(resp_json)
    visible_messages = codex_chat_body.get("messages")
    if not isinstance(visible_messages, list):
        return
    next_visible_messages = list(visible_messages)
    if assistant_msg is not None:
        next_visible_messages.append(assistant_msg)

    next_visible_payload = _build_codex_responses_payload_from_chat(
        _build_codex_chat_body_with_messages(codex_chat_body, next_visible_messages),
        model,
        model_suffix_effort=model_suffix_effort,
    )
    next_fp = _codex_context_fingerprint(next_visible_payload)

    assistant_input_items: List[Dict[str, Any]] = []
    if assistant_msg is not None:
        assistant_payload = _build_codex_responses_payload_from_chat(
            _build_codex_chat_body_with_messages(codex_chat_body, [assistant_msg]),
            model,
            model_suffix_effort=model_suffix_effort,
        )
        raw_assistant_input = assistant_payload.get("input")
        if isinstance(raw_assistant_input, list):
            assistant_input_items = [x for x in raw_assistant_input if isinstance(x, dict)]

    reasoning_items = _extract_codex_reasoning_encrypted_items(resp_json)
    decorated_next_input = copy.deepcopy(sent_input) + reasoning_items + assistant_input_items

    _REASONING_STORE.set_decorated_prefix((session_id, provider, model), next_fp, decorated_next_input)


def _extract_codex_visible_output_items_for_next_input(resp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = resp_json.get("output")
    if not isinstance(out, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in out:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") == "reasoning":
            continue
        if str(item.get("type") or "") == "message":
            content = item.get("content")
            if isinstance(content, list):
                normalized_content = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    p_type = str(part.get("type") or "")
                    if p_type in {"output_text", "text"}:
                        normalized_content.append({"type": "output_text", "text": str(part.get("text") or "")})
                if normalized_content:
                    items.append({"role": "assistant", "content": normalized_content})
            continue
        items.append(copy.deepcopy(item))
    return items


def _maybe_reinject_codex_reasoning_for_responses(
    *,
    session_id: Optional[str],
    provider: str,
    model: str,
    payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if not session_id:
        return payload, None
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return payload, None

    prefix_items, suffix_user_items = _split_trailing_user_suffix_responses_input(input_items)
    prefix_payload = dict(payload)
    prefix_payload["input"] = prefix_items
    fp = _codex_context_fingerprint(prefix_payload)

    decorated_prefix_input = _REASONING_STORE.get_decorated_prefix((session_id, provider, model), fp)

    out_payload = dict(payload)
    if decorated_prefix_input is not None:
        out_payload["input"] = copy.deepcopy(decorated_prefix_input) + copy.deepcopy(suffix_user_items)

    trace = {
        "session_id": session_id,
        "provider": provider,
        "model": model,
        "sent_payload": copy.deepcopy(out_payload),
        "sent_input": copy.deepcopy(out_payload.get("input") if isinstance(out_payload.get("input"), list) else []),
    }
    return out_payload, trace


def _update_codex_reasoning_reinject_cache_for_responses(trace: Optional[Dict[str, Any]], resp_json: Dict[str, Any]) -> None:
    if not trace:
        return
    session_id = trace.get("session_id")
    provider = trace.get("provider")
    model = trace.get("model")
    sent_payload = trace.get("sent_payload")
    sent_input = trace.get("sent_input")
    if not (isinstance(session_id, str) and session_id and isinstance(provider, str) and isinstance(model, str)):
        return
    if not isinstance(sent_payload, dict) or not isinstance(sent_input, list):
        return

    assistant_items = _extract_codex_visible_output_items_for_next_input(resp_json)
    reasoning_items = _extract_codex_reasoning_encrypted_items(resp_json)

    next_payload = dict(sent_payload)
    next_payload["input"] = copy.deepcopy(sent_input) + copy.deepcopy(assistant_items)
    next_fp = _codex_context_fingerprint(next_payload)

    decorated_next_input = copy.deepcopy(sent_input) + reasoning_items + assistant_items
    _REASONING_STORE.set_decorated_prefix((session_id, provider, model), next_fp, decorated_next_input)


def _extract_response_completed_object_from_sse_chunks(chunks: List[Any]) -> Optional[Dict[str, Any]]:
    merged = "".join(chunk for chunk in chunks if isinstance(chunk, str))
    if not merged:
        return None
    merged = merged.replace("\r\n", "\n")
    completed: Optional[Dict[str, Any]] = None
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
        data_part = "\n".join(data_lines).strip()
        if not data_part or data_part == "[DONE]":
            continue
        try:
            obj = json.loads(data_part)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if str(obj.get("type") or "") == "response.completed":
            resp = obj.get("response")
            if isinstance(resp, dict):
                completed = resp
    return completed

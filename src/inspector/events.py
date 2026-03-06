from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src.observability.turn_logging import build_session_non_stream_openai_chat


def truncate_text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def extract_tool_definitions(req_obj: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    tools = req_obj.get("tools")
    if not isinstance(tools, list):
        return out

    for item in tools:
        if not isinstance(item, dict):
            continue

        # anthropic style: {name, description, input_schema}
        name = str(item.get("name") or "").strip()
        if name:
            out[name] = {
                "name": name,
                "description": str(item.get("description") or ""),
                "parameters": item.get("input_schema") if item.get("input_schema") is not None else {},
            }
            continue

        # openai chat style: {type:function, function:{name, description, parameters}}
        if str(item.get("type") or "") == "function" and isinstance(item.get("function"), dict):
            fn = item["function"]
            fn_name = str(fn.get("name") or "").strip()
            if not fn_name:
                continue
            out[fn_name] = {
                "name": fn_name,
                "description": str(fn.get("description") or ""),
                "parameters": fn.get("parameters") if fn.get("parameters") is not None else {},
            }
            continue

        # responses style: {type:function, name, description, parameters}
        if str(item.get("type") or "") == "function":
            fn_name = str(item.get("name") or "").strip()
            if not fn_name:
                continue
            out[fn_name] = {
                "name": fn_name,
                "description": str(item.get("description") or ""),
                "parameters": item.get("parameters") if item.get("parameters") is not None else {},
            }

    return out


def _normalize_non_stream_obj(obj: Dict[str, Any], downstream_format: str) -> Dict[str, Any]:
    # Already normalized chat completion
    if isinstance(obj.get("choices"), list) and obj.get("object") == "chat.completion":
        return obj
    # Typical stored wrappers: {json:{...}}
    if isinstance(obj.get("json"), dict):
        return build_session_non_stream_openai_chat(obj, downstream_format)

    return build_session_non_stream_openai_chat(obj, downstream_format)


def _parse_tool_arguments(raw_args: Any) -> Any:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            return parsed
        except Exception:
            return {"_raw": raw_args}
    return raw_args


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
            if p_type in {"text", "input_text", "output_text"}:
                parts.append(str(part.get("text") or ""))
                continue
            if part.get("text") is not None:
                parts.append(str(part.get("text") or ""))
                continue
            if part.get("content") is not None:
                nested = _extract_text_from_content(part.get("content"))
                if nested:
                    parts.append(nested)
        return "".join(parts)
    if isinstance(content, dict):
        if content.get("text") is not None:
            return str(content.get("text") or "")
        if content.get("content") is not None:
            return _extract_text_from_content(content.get("content"))
    return ""


def _extract_text_from_function_call_output(output: Any) -> str:
    text = _extract_text_from_content(output)
    if text:
        return text

    if isinstance(output, (dict, list)):
        try:
            return json.dumps(output, ensure_ascii=False)
        except Exception:
            return str(output)

    if output is None:
        return ""
    return str(output)


def _extract_tail_request_summaries(req_obj: Dict[str, Any], downstream_format: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    if downstream_format == "openai_responses":
        input_items = req_obj.get("input") if isinstance(req_obj.get("input"), list) else []
        for item in reversed(input_items):
            if not isinstance(item, dict):
                break
            if str(item.get("type") or "") == "function_call_output":
                text = _extract_text_from_function_call_output(item.get("output"))
                if not text:
                    break
                out.append({"kind": "tool_result", "summary": text})
                continue
            role = str(item.get("role") or "")
            if role == "assistant":
                break
            if role not in {"user", "tool", "developer"}:
                break
            text = _extract_text_from_content(item.get("content"))
            if not text:
                break
            kind = "tool_result" if role == "tool" else "user_input"
            out.append({"kind": kind, "summary": text})
        return out

    messages = req_obj.get("messages") if isinstance(req_obj.get("messages"), list) else []
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            break
        role = str(msg.get("role") or "")
        if role == "assistant":
            break
        if role not in {"user", "tool", "developer"}:
            break

        content = msg.get("content")
        if role == "user" and isinstance(content, list):
            # anthropic tool_result 常见于 user role；若命中则按工具返回显示。
            tool_result_texts: List[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if str(part.get("type") or "") != "tool_result":
                    continue
                text = _extract_text_from_content(part.get("content"))
                if text:
                    tool_result_texts.append(text)
            if tool_result_texts:
                out.append({"kind": "tool_result", "summary": "".join(tool_result_texts)})
                continue

        text = _extract_text_from_content(content)
        if not text:
            break
        kind = "tool_result" if role == "tool" else "user_input"
        out.append({"kind": kind, "summary": text})
    return out


def build_request_events(
    *,
    turn_ts: str,
    lane_id: str,
    downstream_format: str,
    req_obj: Dict[str, Any],
    summary_chars: int,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    tail_requests = _extract_tail_request_summaries(req_obj, downstream_format)
    ordered_requests = list(reversed(tail_requests))

    for idx, req in enumerate(ordered_requests):
        kind = str(req.get("kind") or "user_input")
        summary = str(req.get("summary") or "")
        if not summary:
            continue
        out.append(
            {
                "event_id": f"{turn_ts}:request:{idx}",
                "ts": turn_ts,
                "lane_id": lane_id,
                "kind": kind,
                "summary": truncate_text(summary, summary_chars),
                "detail": {"summary_text": summary},
                "tool_name": None,
                "tool_args": None,
                "tool_def": None,
                "turn_ts": turn_ts,
                "format": downstream_format,
            }
        )
    return out


def build_response_events(
    *,
    turn_ts: str,
    lane_id: str,
    downstream_format: str,
    non_stream_obj: Optional[Dict[str, Any]],
    downstream_obj: Optional[Dict[str, Any]],
    tool_defs: Dict[str, Dict[str, Any]],
    summary_chars: int,
) -> List[Dict[str, Any]]:
    if non_stream_obj is None and downstream_obj is None:
        return []

    normalized: Optional[Dict[str, Any]] = None
    if non_stream_obj is not None:
        normalized = _normalize_non_stream_obj(non_stream_obj, downstream_format)
    elif downstream_obj is not None:
        normalized = _normalize_non_stream_obj(downstream_obj, downstream_format)

    events: List[Dict[str, Any]] = []
    message = {}
    if isinstance(normalized, dict):
        choices = normalized.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}

    content = str(message.get("content") or "") if isinstance(message, dict) else ""
    if content:
        events.append(
            {
                "event_id": f"{turn_ts}:assistant_text:0",
                "ts": turn_ts,
                "lane_id": lane_id,
                "kind": "assistant_text",
                "summary": truncate_text(content, summary_chars),
                "detail": {"content": content},
                "tool_name": None,
                "tool_args": None,
                "tool_def": None,
                "turn_ts": turn_ts,
                "format": downstream_format,
            }
        )

    reasoning = ""
    if isinstance(message, dict) and isinstance(message.get("reasoning_content"), str):
        reasoning = str(message.get("reasoning_content") or "")
    if reasoning:
        events.append(
            {
                "event_id": f"{turn_ts}:assistant_reasoning:0",
                "ts": turn_ts,
                "lane_id": lane_id,
                "kind": "assistant_reasoning",
                "summary": truncate_text(reasoning, summary_chars),
                "detail": {"reasoning_content": reasoning},
                "tool_name": None,
                "tool_args": None,
                "tool_def": None,
                "turn_ts": turn_ts,
                "format": downstream_format,
            }
        )

    tool_calls = message.get("tool_calls") if isinstance(message, dict) else []
    if isinstance(tool_calls, list):
        for idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            tool_name = str(fn.get("name") or "unknown_tool")
            tool_args = _parse_tool_arguments(fn.get("arguments"))
            tool_def = tool_defs.get(tool_name)
            events.append(
                {
                    "event_id": f"{turn_ts}:tool_call:{idx}",
                    "ts": turn_ts,
                    "lane_id": lane_id,
                    "kind": "tool_call",
                    "summary": tool_name,
                    "detail": tc,
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "tool_def": tool_def,
                    "turn_ts": turn_ts,
                    "format": downstream_format,
                }
            )

    if not events and downstream_obj is not None:
        # Fallback event for error-only responses
        status_code = downstream_obj.get("status_code")
        error_summary = f"response status {status_code}" if status_code is not None else "response event"
        events.append(
            {
                "event_id": f"{turn_ts}:response_status:0",
                "ts": turn_ts,
                "lane_id": lane_id,
                "kind": "response_status",
                "summary": truncate_text(error_summary, summary_chars),
                "detail": downstream_obj,
                "tool_name": None,
                "tool_args": None,
                "tool_def": None,
                "turn_ts": turn_ts,
                "format": downstream_format,
            }
        )

    return events

import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from proxy_converters import _extract_text_from_blocks


def _dump_json(path: str, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _resp_to_obj(r):  # httpx.Response -> dict
    base = {"status_code": r.status_code, "headers": dict(r.headers)}
    try:
        base["json"] = r.json()
    except Exception:
        base["text"] = r.text
    return base


def _extract_first_user_text(body: Dict[str, Any]) -> str:
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return ""
    first = msgs[0] or {}
    content = first.get("content")
    return _extract_text_from_blocks(content).strip().lower()


def _system_texts(body: Dict[str, Any]) -> List[str]:
    systems = body.get("system")
    if systems is None:
        return []
    if isinstance(systems, list):
        texts = []
        for s in systems:
            if isinstance(s, dict):
                texts.append(_extract_text_from_blocks(s.get("text")))
            else:
                texts.append(_extract_text_from_blocks(s))
        return texts
    return [_extract_text_from_blocks(systems)]


def _should_skip_session_logging(body: Dict[str, Any]) -> bool:
    first_text = _extract_first_user_text(body)
    if first_text == "warmup":
        return True

    sys_texts = " ".join(t.lower() for t in _system_texts(body))
    if "analyze if this message indicates a new conversation topic" in sys_texts:
        return True
    if "summarize this coding conversation" in sys_texts:
        return True
    return False


def _discard_session_req(session_req_path: Optional[str]) -> None:
    if session_req_path and os.path.exists(session_req_path):
        try:
            os.remove(session_req_path)
        except Exception:
            pass


def _usage_dict_has_tokens(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(k in usage for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"))


def _extract_usage_from_obj(obj: Any) -> Optional[Dict[str, Any]]:
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            return obj.get("usage")
        if isinstance(obj.get("json"), dict) and isinstance(obj["json"].get("usage"), dict):
            return obj["json"].get("usage")
        if isinstance(obj.get("text"), str):
            try:
                parsed = json.loads(obj.get("text"))
                if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                    return parsed.get("usage")
            except Exception:
                pass
    return None


def _parse_anthropic_sse_chunks_to_events(chunks: List[Any]) -> List[Dict[str, Any]]:
    raw_text = []
    for chunk in chunks:
        if isinstance(chunk, bytes):
            raw_text.append(chunk.decode("utf-8", errors="replace"))
        elif isinstance(chunk, str):
            raw_text.append(chunk)
    text = "".join(raw_text)
    lines = text.splitlines()
    events: List[Dict[str, Any]] = []
    current_event = None
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            current_event = None
            continue
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
            continue
        if line.startswith("data:"):
            data_str = line[len("data:") :].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                data = {"_raw": data_str}
            event = current_event
            if event is None and isinstance(data, dict):
                event = data.get("type")
            events.append({"event": event, "data": data})
    return events


def _build_anthropic_non_stream_from_events(
    events: List[Dict[str, Any]],
    fallback_model: str,
) -> Optional[Dict[str, Any]]:
    if not events:
        return None
    resp: Dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": fallback_model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
    }
    blocks: Dict[int, Dict[str, Any]] = {}
    input_buffers: Dict[int, str] = {}

    for ev in events:
        event = ev.get("event")
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        if not event:
            event = data.get("type")

        if event == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                resp["id"] = msg.get("id") or resp["id"]
                resp["role"] = msg.get("role") or resp["role"]
                resp["model"] = msg.get("model") or resp["model"]
                resp["stop_reason"] = msg.get("stop_reason")
                resp["stop_sequence"] = msg.get("stop_sequence")
                if isinstance(msg.get("usage"), dict):
                    resp["usage"] = dict(msg.get("usage"))
            continue

        if event == "content_block_start":
            idx = data.get("index")
            cb = data.get("content_block") or {}
            if idx is None or not isinstance(cb, dict):
                continue
            block = dict(cb)
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "thinking":
                block.setdefault("thinking", "")
            elif block.get("type") == "tool_use":
                block.setdefault("input", {})
                input_buffers[idx] = ""
            blocks[idx] = block
            continue

        if event == "content_block_delta":
            idx = data.get("index")
            delta = data.get("delta") or {}
            if idx is None or not isinstance(delta, dict):
                continue
            block = blocks.get(idx)
            if not block:
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif delta_type == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
            elif delta_type == "input_json_delta":
                input_buffers[idx] = input_buffers.get(idx, "") + (delta.get("partial_json") or "")
            continue

        if event == "message_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict):
                if delta.get("stop_reason") is not None:
                    resp["stop_reason"] = delta.get("stop_reason")
            usage = data.get("usage")
            if isinstance(usage, dict):
                resp["usage"] = dict(usage)
            continue

    for idx, buf in input_buffers.items():
        if not buf:
            continue
        block = blocks.get(idx)
        if not block or block.get("type") != "tool_use":
            continue
        try:
            block["input"] = json.loads(buf)
        except Exception:
            block["input"] = {"_raw_input": buf}

    if blocks:
        resp["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return resp


def _sse_event(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return (f"event: {event}\n" f"data: {payload}\n\n").encode("utf-8")


def _collect_usage_tokens(obj: Any) -> Tuple[int, int]:
    def _usage_pair(usage: Any) -> Tuple[int, int]:
        if not isinstance(usage, dict):
            return 0, 0
        try:
            in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            return in_tok, out_tok
        except Exception:
            return 0, 0

    total_in, total_out = 0, 0
    direct_usage = None
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            direct_usage = obj.get("usage")
        elif isinstance(obj.get("json"), dict):
            direct_usage = obj.get("json", {}).get("usage")

    if direct_usage is not None:
        return _usage_pair(direct_usage)

    usage_found = False
    if isinstance(obj, dict):
        events = obj.get("events")
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue
                in_tok, out_tok = _usage_pair(data.get("usage"))
                if in_tok or out_tok:
                    usage_found = True
                total_in += in_tok
                total_out += out_tok

        if not usage_found:
            last_in = 0
            last_out = 0
            chunks = obj.get("chunks")
            if isinstance(chunks, list):
                for chunk in chunks:
                    if isinstance(chunk, dict):
                        in_tok, out_tok = _usage_pair(chunk.get("usage"))
                        if in_tok or out_tok:
                            last_in, last_out = in_tok, out_tok
                        if isinstance(chunk.get("json"), dict):
                            in_tok, out_tok = _usage_pair(chunk.get("json", {}).get("usage"))
                            if in_tok or out_tok:
                                last_in, last_out = in_tok, out_tok
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
                    in_tok, out_tok = _usage_pair(data.get("usage"))
                    if in_tok or out_tok:
                        last_in, last_out = in_tok, out_tok
                    choices = data.get("choices")
                    if isinstance(choices, list) and choices:
                        c0 = choices[0] or {}
                        if isinstance(c0, dict):
                            in_tok, out_tok = _usage_pair(c0.get("usage"))
                            if in_tok or out_tok:
                                last_in, last_out = in_tok, out_tok
            total_in += last_in
            total_out += last_out

    return total_in, total_out

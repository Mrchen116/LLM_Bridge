from __future__ import annotations

import json
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.inspector.canonicalize import (
    canonical_context_from_req,
    context_fingerprint,
    first_user_text_for_label,
    infer_downstream_format,
)
from src.inspector.events import (
    build_request_event,
    build_response_events,
    extract_tool_definitions,
)
from src.inspector.files import (
    build_turn_file_index,
    find_session_dirs_by_id,
    list_session_dirs,
    parse_session_dir_name,
)
from src.inspector.grouping import AssignedLane, TurnLaneInput, assign_lanes, lane_sort_key
from src.inspector.types import Lane, SessionSummary, TimelineEvent


@dataclass(frozen=True)
class _TurnRecord:
    ts: str
    downstream_format: str
    req_obj: Dict[str, Any]
    req_file: str
    non_stream_obj: Optional[Dict[str, Any]]
    non_stream_file: Optional[str]
    downstream_obj: Optional[Dict[str, Any]]
    downstream_file: Optional[str]
    lane_key: str
    lane: AssignedLane


@dataclass
class _LaneMatchState:
    lane_key: str
    static_key: str
    last_prefix_tokens: Tuple[str, ...]


def _to_workspace_relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _serialize_token(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _list_session_summaries(logs_session_dir: str, q: Optional[str]) -> List[SessionSummary]:
    query = (q or "").strip().lower()
    out: List[SessionSummary] = []
    for session_dir in list_session_dirs(logs_session_dir):
        parsed = parse_session_dir_name(session_dir.name)
        if not parsed:
            continue
        dir_ts, session_id = parsed
        if query and query not in session_id.lower():
            continue

        index = build_turn_file_index(session_dir)
        req_turns = [ts for ts, slots in index.items() if "req" in slots]
        if req_turns:
            start_ts = min(req_turns)
            end_ts = max(req_turns)
        else:
            start_ts = dir_ts
            end_ts = dir_ts

        formats = set()
        for slots in index.values():
            req_entry = slots.get("req")
            if not req_entry:
                continue
            req_format, req_path = req_entry
            req_obj = _read_json(req_path)
            fmt = infer_downstream_format(req_obj, req_format)
            formats.add(fmt)

        out.append(
            SessionSummary(
                session_id=session_id,
                session_dir=session_dir.name,
                start_ts=start_ts,
                end_ts=end_ts,
                turn_count=len(req_turns),
                formats=sorted(formats),
            )
        )

    out.sort(key=lambda x: x.session_dir, reverse=True)
    return out


def list_sessions(
    *,
    logs_session_dir: str,
    limit: int,
    cursor: Optional[str],
    q: Optional[str],
) -> Dict[str, Any]:
    all_items = _list_session_summaries(logs_session_dir, q)

    start_idx = 0
    if cursor:
        for i, item in enumerate(all_items):
            if item.session_dir == cursor:
                start_idx = i + 1
                break

    sliced = all_items[start_idx : start_idx + max(1, limit)]
    next_cursor = None
    if start_idx + len(sliced) < len(all_items):
        next_cursor = sliced[-1].session_dir

    return {
        "items": [
            {
                "session_id": x.session_id,
                "session_dir": x.session_dir,
                "start_ts": x.start_ts,
                "end_ts": x.end_ts,
                "turn_count": x.turn_count,
                "formats": x.formats,
            }
            for x in sliced
        ],
        "next_cursor": next_cursor,
    }


def _resolve_session_dir(logs_session_dir: str, session_id: str) -> Optional[Path]:
    dirs = find_session_dirs_by_id(logs_session_dir, session_id)
    if not dirs:
        return None
    # Keep the same behavior style as existing stats API: deterministic first match.
    return dirs[0]


def _build_turn_records(
    *,
    session_dir: Path,
    summary_chars: int,
) -> Tuple[List[_TurnRecord], List[str]]:
    del summary_chars  # reserved for future, keep API stable for helper signature

    warnings: List[str] = []
    index = build_turn_file_index(session_dir)
    turns_meta: List[Tuple[str, str, Dict[str, Any], str, str, str, str, Tuple[str, ...]]] = []
    # tuple:
    # ts, fmt, req_obj, lane_key_or_hint, label_hint, req_file, static_key, prefix_tokens

    for ts in sorted(index.keys()):
        slots = index[ts]
        req_entry = slots.get("req")
        if not req_entry:
            continue
        req_format, req_path = req_entry
        req_obj = _read_json(req_path)
        fmt = infer_downstream_format(req_obj, req_format)
        req_file = _to_workspace_relative(req_path)

        provider = str(req_obj.get("_upstream_provider") or "unknown")
        try:
            context_key, model = canonical_context_from_req(req_obj, fmt)
            input_items = context_key.get("input")
            if not isinstance(input_items, list):
                input_items = []
            prefix_tokens = tuple(_serialize_token(item) for item in input_items)

            static_ctx = {k: v for k, v in context_key.items() if k != "input"}
            static_key = f"{provider}|{model}|static:{context_fingerprint(static_ctx)}"
            lane_key_or_hint = ""
        except Exception as exc:
            # Fallback: still keep turn visible.
            warnings.append(f"turn {ts}: context fingerprint fallback due to {exc}")
            text_hint = first_user_text_for_label(req_obj, fmt)
            fallback_base = f"{provider}|{req_obj.get('model')}|{text_hint}" if text_hint else f"{provider}|unknown"
            fp = context_fingerprint({"fallback": fallback_base})
            static_key = f"{provider}|fallback|{fp}"
            prefix_tokens = tuple()
            lane_key_or_hint = f"{provider}|fallback|{fp}"

        label_hint = first_user_text_for_label(req_obj, fmt)
        turns_meta.append(
            (ts, fmt, req_obj, lane_key_or_hint, label_hint, req_file, static_key, prefix_tokens)
        )

    # Prefix-chain lane matching:
    # same static config (instructions/tools/tool_choice/etc.) + previous prefix
    # being a prefix of current prefix => same lane.
    lane_states: List[_LaneMatchState] = []
    next_lane_idx = 1
    resolved_meta: List[Tuple[str, str, Dict[str, Any], str, str, str]] = []
    for ts, fmt, req_obj, lane_key_or_hint, label_hint, req_file, static_key, prefix_tokens in turns_meta:
        lane_key = lane_key_or_hint
        if not lane_key:
            best_idx = -1
            best_prefix_len = -1
            for idx, lane_state in enumerate(lane_states):
                if lane_state.static_key != static_key:
                    continue
                prior = lane_state.last_prefix_tokens
                if len(prior) > len(prefix_tokens):
                    continue
                if prefix_tokens[: len(prior)] != prior:
                    continue
                if len(prior) > best_prefix_len:
                    best_idx = idx
                    best_prefix_len = len(prior)

            if best_idx >= 0:
                lane_key = lane_states[best_idx].lane_key
                if len(prefix_tokens) > len(lane_states[best_idx].last_prefix_tokens):
                    lane_states[best_idx].last_prefix_tokens = prefix_tokens
            else:
                lane_key = f"{static_key}|lane:{next_lane_idx}"
                lane_states.append(
                    _LaneMatchState(
                        lane_key=lane_key,
                        static_key=static_key,
                        last_prefix_tokens=prefix_tokens,
                    )
                )
                next_lane_idx += 1

        resolved_meta.append((ts, fmt, req_obj, lane_key, label_hint, req_file))

    lane_map = assign_lanes(
        TurnLaneInput(ts=ts, lane_key=lane_key, label_hint=label_hint)
        for ts, _fmt, _req, lane_key, label_hint, _req_file in resolved_meta
    )

    records: List[_TurnRecord] = []
    for ts, fmt, req_obj, lane_key, _label_hint, req_file in resolved_meta:
        slots = index[ts]
        non_stream_obj = None
        non_stream_file = None
        downstream_obj = None
        downstream_file = None

        non_stream_entry = slots.get("non_stream")
        if non_stream_entry:
            _non_stream_format, non_stream_path = non_stream_entry
            non_stream_obj = _read_json(non_stream_path)
            non_stream_file = _to_workspace_relative(non_stream_path)

        downstream_entry = slots.get("downstream")
        if downstream_entry:
            _downstream_format, downstream_path = downstream_entry
            downstream_obj = _read_json(downstream_path)
            downstream_file = _to_workspace_relative(downstream_path)

        lane = lane_map.get(lane_key)
        if lane is None:
            warnings.append(f"turn {ts}: lane map missing, using synthetic lane")
            lane = AssignedLane(
                lane_id=hashlib.sha1(lane_key.encode("utf-8")).hexdigest()[:12],
                label="Agent ? · (unknown)",
            )

        records.append(
            _TurnRecord(
                ts=ts,
                downstream_format=fmt,
                req_obj=req_obj,
                req_file=req_file,
                non_stream_obj=non_stream_obj,
                non_stream_file=non_stream_file,
                downstream_obj=downstream_obj,
                downstream_file=downstream_file,
                lane_key=lane_key,
                lane=lane,
            )
        )

    return records, warnings


def _matches_event_query(event: Dict[str, Any], q: str) -> bool:
    if not q:
        return True
    needle = q.lower()
    hay_summary = str(event.get("summary") or "").lower()
    if needle in hay_summary:
        return True
    try:
        hay_detail = json.dumps(event.get("detail"), ensure_ascii=False).lower()
    except Exception:
        hay_detail = str(event.get("detail") or "").lower()
    return needle in hay_detail


def get_timeline(
    *,
    logs_session_dir: str,
    session_id: str,
    include_non_tool: bool,
    agent: Optional[str],
    tool: Optional[str],
    q: Optional[str],
    summary_chars: int,
) -> Optional[Dict[str, Any]]:
    session_dir = _resolve_session_dir(logs_session_dir, session_id)
    if not session_dir:
        return None

    records, warnings = _build_turn_records(session_dir=session_dir, summary_chars=summary_chars)

    raw_events: List[Dict[str, Any]] = []
    seq = 0
    for rec in sorted(records, key=lambda x: x.ts):
        tool_defs = extract_tool_definitions(rec.req_obj)
        source_files = {
            "request": rec.req_file,
            "response": rec.non_stream_file or rec.downstream_file,
            "non_stream_response": rec.non_stream_file,
            "downstream_response": rec.downstream_file,
        }

        req_event = build_request_event(
            turn_ts=rec.ts,
            lane_id=rec.lane.lane_id,
            downstream_format=rec.downstream_format,
            req_obj=rec.req_obj,
            summary_chars=summary_chars,
        )
        if req_event is not None:
            req_event["_seq"] = seq
            req_event["source_files"] = source_files
            raw_events.append(req_event)
            seq += 1

        for ev in build_response_events(
            turn_ts=rec.ts,
            lane_id=rec.lane.lane_id,
            downstream_format=rec.downstream_format,
            non_stream_obj=rec.non_stream_obj,
            downstream_obj=rec.downstream_obj,
            tool_defs=tool_defs,
            summary_chars=summary_chars,
        ):
            ev["_seq"] = seq
            ev["source_files"] = source_files
            raw_events.append(ev)
            seq += 1

    events_filtered: List[Dict[str, Any]] = []
    agent_filter = (agent or "").strip().lower()
    tool_filter = (tool or "").strip()
    query = (q or "").strip()

    lane_id_to_label = {rec.lane.lane_id: rec.lane.label for rec in records}

    for ev in raw_events:
        if not include_non_tool and ev.get("kind") != "tool_call":
            continue

        if agent_filter:
            lane_id = str(ev.get("lane_id") or "")
            lane_label = lane_id_to_label.get(lane_id, "")
            if agent_filter != lane_id.lower() and agent_filter not in lane_label.lower():
                continue

        if tool_filter and ev.get("kind") == "tool_call":
            if str(ev.get("tool_name") or "") != tool_filter:
                continue
        elif tool_filter and ev.get("kind") != "tool_call":
            continue

        if query and not _matches_event_query(ev, query):
            continue

        events_filtered.append(ev)

    events_filtered.sort(key=lambda x: (x.get("ts") or "", int(x.get("_seq") or 0)))

    lane_stats: Dict[str, Dict[str, Any]] = {}
    for ev in events_filtered:
        lane_id = str(ev.get("lane_id") or "")
        stat = lane_stats.setdefault(
            lane_id,
            {
                "lane_id": lane_id,
                "label": lane_id_to_label.get(lane_id, lane_id),
                "event_count": 0,
                "first_ts": ev.get("ts"),
                "last_ts": ev.get("ts"),
            },
        )
        stat["event_count"] += 1
        stat["last_ts"] = ev.get("ts")

    lanes: List[Lane] = []
    for lane_id, stat in lane_stats.items():
        lanes.append(
            Lane(
                lane_id=lane_id,
                label=str(stat.get("label") or lane_id),
                event_count=int(stat.get("event_count") or 0),
                first_ts=str(stat.get("first_ts") or ""),
                last_ts=str(stat.get("last_ts") or ""),
            )
        )
    lanes.sort(key=lambda x: lane_sort_key(x.label))

    out_events: List[TimelineEvent] = []
    for ev in events_filtered:
        ev.pop("_seq", None)
        out_events.append(
            TimelineEvent(
                event_id=str(ev.get("event_id") or ""),
                ts=str(ev.get("ts") or ""),
                lane_id=str(ev.get("lane_id") or ""),
                kind=str(ev.get("kind") or ""),
                summary=str(ev.get("summary") or ""),
                detail=ev.get("detail"),
                tool_name=ev.get("tool_name"),
                tool_args=ev.get("tool_args"),
                tool_def=ev.get("tool_def"),
                source_files=ev.get("source_files"),
                turn_ts=str(ev.get("turn_ts") or ""),
                format=str(ev.get("format") or ""),
            )
        )

    tool_events = sum(1 for ev in out_events if ev.kind == "tool_call")
    total_events = len(out_events)

    return {
        "session_id": session_id,
        "session_dir": session_dir.name,
        "lanes": [
            {
                "lane_id": x.lane_id,
                "label": x.label,
                "event_count": x.event_count,
                "first_ts": x.first_ts,
                "last_ts": x.last_ts,
            }
            for x in lanes
        ],
        "events": [
            {
                "event_id": x.event_id,
                "ts": x.ts,
                "lane_id": x.lane_id,
                "kind": x.kind,
                "summary": x.summary,
                "detail": x.detail,
                "tool_name": x.tool_name,
                "tool_args": x.tool_args,
                "tool_def": x.tool_def,
                "source_files": x.source_files,
                "turn_ts": x.turn_ts,
                "format": x.format,
            }
            for x in out_events
        ],
        "stats": {
            "total_events": total_events,
            "tool_events": tool_events,
            "non_tool_events": total_events - tool_events,
            "lane_count": len(lanes),
        },
        "meta": {
            "warnings": warnings,
            "summary_chars": summary_chars,
        },
    }

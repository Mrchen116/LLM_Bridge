from __future__ import annotations

import json
from dataclasses import dataclass
import hashlib
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from src.inspector.canonicalize import (
    canonical_context_from_req,
    context_fingerprint,
    first_user_text_for_label,
    infer_downstream_format,
)
from src.inspector.events import (
    build_request_events,
    build_response_events,
    extract_tool_definitions,
)
from src.inspector.files import (
    build_turn_file_index,
    find_session_dirs_by_id,
    list_req_files,
    list_session_dirs,
    parse_ts_to_epoch_ms,
    parse_session_dir_name,
    session_dir_signature,
)
from src.inspector.grouping import AssignedLane, TurnLaneInput, assign_lanes, lane_sort_key
from src.inspector.types import Lane, SessionSummary, TimelineEvent, session_summary_to_dict
from src.observability.token_stats import collect_usage_tokens_for_stats, compute_token_breakdown


@dataclass(frozen=True)
class _TurnRecord:
    ts: str
    request_started_at_ms: int
    response_completed_at_ms: int
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


@dataclass(frozen=True)
class _SessionSummaryCacheEntry:
    signature: Tuple[int, int]
    summary: SessionSummary


@dataclass(frozen=True)
class _RawTimelineBundle:
    signature: Tuple[int, int]
    summary_chars: int
    session_dir_name: str
    records: List[_TurnRecord]
    warnings: List[str]
    raw_events: List[Dict[str, Any]]
    lane_id_to_label: Dict[str, str]


@dataclass(frozen=True)
class _FilteredTimelineCacheEntry:
    key: Tuple[Any, ...]
    payload: Dict[str, Any]


_SESSION_SUMMARY_CACHE: Dict[str, _SessionSummaryCacheEntry] = {}
_RAW_TIMELINE_CACHE: Dict[Tuple[str, int], _RawTimelineBundle] = {}
_FILTERED_TIMELINE_CACHE: Dict[Tuple[str, Tuple[Any, ...]], _FilteredTimelineCacheEntry] = {}


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


def _now_ms() -> float:
    return time.perf_counter() * 1000


def _elapsed_ms(start_ms: float) -> float:
    return round(_now_ms() - start_ms, 2)


def _copy_jsonish(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return value


def _build_session_summary(session_dir: Path) -> Optional[SessionSummary]:
    parsed = parse_session_dir_name(session_dir.name)
    if not parsed:
        return None
    dir_ts, session_id = parsed
    req_files = list_req_files(session_dir)
    turn_count = len(req_files)
    start_ts = dir_ts
    end_ts = req_files[-1][0] if req_files else dir_ts

    formats: List[str] = []
    if req_files:
        req_format, req_path = req_files[0][1], req_files[0][2]
        req_obj = _read_json(req_path)
        fmt = infer_downstream_format(req_obj, req_format)
        if fmt:
            formats = [fmt]

    return SessionSummary(
        session_id=session_id,
        session_dir=session_dir.name,
        start_ts=start_ts,
        end_ts=end_ts,
        turn_count=turn_count,
        formats=formats,
    )


def _get_cached_session_summary(session_dir: Path) -> Tuple[Optional[SessionSummary], bool]:
    signature = session_dir_signature(session_dir)
    cache_key = str(session_dir.resolve())
    cached = _SESSION_SUMMARY_CACHE.get(cache_key)
    if cached and cached.signature == signature:
        return cached.summary, True

    summary = _build_session_summary(session_dir)
    if summary is None:
        return None, False
    _SESSION_SUMMARY_CACHE[cache_key] = _SessionSummaryCacheEntry(signature=signature, summary=summary)
    return summary, False


def list_sessions(
    *,
    logs_session_dir: str,
    limit: int,
    page: int,
    q: Optional[str],
) -> Dict[str, Any]:
    total_start_ms = _now_ms()
    query = (q or "").strip().lower()
    scan_start_ms = _now_ms()
    all_dirs = list_session_dirs(logs_session_dir)
    filtered_dirs = [
        session_dir
        for session_dir in all_dirs
        if (parsed := parse_session_dir_name(session_dir.name))
        and (not query or query in parsed[1].lower())
    ]
    dir_scan_ms = _elapsed_ms(scan_start_ms)
    page_size = max(1, limit)
    total_items = len(filtered_dirs)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    current_page = min(max(1, page), total_pages)
    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    sliced_dirs = filtered_dirs[start_idx:end_idx]
    summary_start_ms = _now_ms()
    items: List[SessionSummary] = []
    summary_cache_hits = 0
    for session_dir in sliced_dirs:
        summary, cache_hit = _get_cached_session_summary(session_dir)
        if summary is None:
            continue
        if cache_hit:
            summary_cache_hits += 1
        items.append(summary)
    summary_build_ms = _elapsed_ms(summary_start_ms)

    return {
        "items": [session_summary_to_dict(x) for x in items],
        "page": current_page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_prev": current_page > 1,
        "has_next": end_idx < total_items,
        "meta": {
            "perf": {
                "dir_scan_ms": dir_scan_ms,
                "summary_build_ms": summary_build_ms,
                "total_ms": _elapsed_ms(total_start_ms),
            },
            "cache": {
                "summary_hits": summary_cache_hits,
                "summary_misses": max(0, len(sliced_dirs) - summary_cache_hits),
            },
            "counts": {
                "total_dirs": len(all_dirs),
                "filtered_dirs": len(filtered_dirs),
                "returned_items": len(items),
            },
        },
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
        non_stream_path: Optional[Path] = None
        downstream_obj = None
        downstream_file = None
        downstream_path: Optional[Path] = None

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

        request_started_at_ms = parse_ts_to_epoch_ms(ts)
        response_completed_at_ms = _extract_response_completed_at_ms(
            non_stream_obj if non_stream_path is not None else downstream_obj,
            non_stream_path if non_stream_path is not None else downstream_path,
            request_started_at_ms,
        )

        records.append(
            _TurnRecord(
                ts=ts,
                request_started_at_ms=request_started_at_ms,
                response_completed_at_ms=response_completed_at_ms,
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


def _build_raw_events_bundle(session_dir: Path, summary_chars: int) -> _RawTimelineBundle:
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

        req_events = build_request_events(
            turn_ts=rec.ts,
            lane_id=rec.lane.lane_id,
            downstream_format=rec.downstream_format,
            req_obj=rec.req_obj,
            summary_chars=summary_chars,
        )
        for req_event in req_events:
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

    return _RawTimelineBundle(
        signature=session_dir_signature(session_dir),
        summary_chars=summary_chars,
        session_dir_name=session_dir.name,
        records=records,
        warnings=warnings,
        raw_events=raw_events,
        lane_id_to_label={rec.lane.lane_id: rec.lane.label for rec in records},
    )


def _get_raw_timeline_bundle(
    session_dir: Path, summary_chars: int
) -> Tuple[_RawTimelineBundle, bool]:
    signature = session_dir_signature(session_dir)
    cache_key = (str(session_dir.resolve()), summary_chars)
    cached = _RAW_TIMELINE_CACHE.get(cache_key)
    if cached and cached.signature == signature:
        return cached, True

    bundle = _build_raw_events_bundle(session_dir=session_dir, summary_chars=summary_chars)
    _RAW_TIMELINE_CACHE[cache_key] = bundle
    return bundle, False


def _parse_keyword_list(raw: Optional[str]) -> List[str]:
    text = (raw or "").strip()
    if not text:
        return []
    separators = [",", "，", "\n", ";", "；"]
    for sep in separators:
        text = text.replace(sep, "\n")
    out: List[str] = []
    seen = set()
    for part in text.split("\n"):
        token = part.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _event_matches_any_keyword(event: Dict[str, Any], keywords: List[str]) -> bool:
    if not keywords:
        return False
    hay_summary = str(event.get("summary") or "").lower()
    try:
        hay_detail = json.dumps(event.get("detail"), ensure_ascii=False).lower()
    except Exception:
        hay_detail = str(event.get("detail") or "").lower()
    return any((kw in hay_summary) or (kw in hay_detail) for kw in keywords)


def _turn_passes_keyword_filter(
    *,
    turn_events: List[Dict[str, Any]],
    include_keywords: List[str],
    exclude_keywords: List[str],
) -> bool:
    if any(_event_matches_any_keyword(event, exclude_keywords) for event in turn_events):
        return False
    if not include_keywords:
        return True
    return any(_event_matches_any_keyword(event, include_keywords) for event in turn_events)


def _new_token_bucket() -> Dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "num_turns": 0,
    }


def _apply_token_usage(bucket: Dict[str, int], obj: Any, path: str) -> None:
    bucket["num_turns"] += 1
    in_tok, out_tok, _fmt = collect_usage_tokens_for_stats(obj, path)
    bucket["input_tokens"] += in_tok
    bucket["output_tokens"] += out_tok


def _sorted_tool_counts(tool_counts: Dict[str, int]) -> List[Dict[str, Any]]:
    return [
        {"tool_name": name, "count": count}
        for name, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _extract_response_completed_at_ms(
    response_obj: Optional[Dict[str, Any]],
    response_path: Optional[Path],
    default_ms: int,
) -> int:
    if isinstance(response_obj, dict):
        meta = response_obj.get("_log_meta")
        if isinstance(meta, dict):
            try:
                value = int(meta.get("response_completed_at_ms") or 0)
            except Exception:
                value = 0
            if value > 0:
                return value

    if response_path is not None:
        try:
            stat_ms = int(response_path.stat().st_mtime * 1000)
        except Exception:
            stat_ms = 0
        if stat_ms > 0:
            return stat_ms

    return default_ms


def _build_duration_stats(
    turn_records: List[_TurnRecord],
    lane_id_to_label: Dict[str, str],
) -> Dict[str, Any]:
    if not turn_records:
        return {
            "session": {
                "start_ms": None,
                "end_ms": None,
                "duration_ms": 0,
            },
            "agents": [],
        }

    session_start_ms = min(rec.request_started_at_ms for rec in turn_records)
    session_end_ms = max(rec.response_completed_at_ms for rec in turn_records)

    lane_ranges: Dict[str, Dict[str, int]] = {}
    for rec in turn_records:
        slot = lane_ranges.setdefault(
            rec.lane.lane_id,
            {
                "start_ms": rec.request_started_at_ms,
                "end_ms": rec.response_completed_at_ms,
            },
        )
        slot["start_ms"] = min(slot["start_ms"], rec.request_started_at_ms)
        slot["end_ms"] = max(slot["end_ms"], rec.response_completed_at_ms)

    agents: List[Dict[str, Any]] = []
    for lane_id in sorted(lane_ranges.keys(), key=lambda x: lane_sort_key(lane_id_to_label.get(x, x))):
        slot = lane_ranges[lane_id]
        start_ms = int(slot.get("start_ms") or 0)
        end_ms = int(slot.get("end_ms") or 0)
        agents.append(
            {
                "lane_id": lane_id,
                "label": lane_id_to_label.get(lane_id, lane_id),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": max(0, end_ms - start_ms),
            }
        )

    return {
        "session": {
            "start_ms": session_start_ms,
            "end_ms": session_end_ms,
            "duration_ms": max(0, session_end_ms - session_start_ms),
        },
        "agents": agents,
    }


def _compact_event_for_timeline(event: Dict[str, Any], include_detail: bool) -> Dict[str, Any]:
    compact = dict(event)
    compact["detail_loaded"] = include_detail
    if include_detail:
        return compact
    compact["detail"] = None
    compact["tool_args"] = None
    compact["tool_def"] = None
    return compact


def get_timeline(
    *,
    logs_session_dir: str,
    session_id: str,
    include_non_tool: bool,
    agent: Optional[str],
    tool: Optional[str],
    q: Optional[str],
    q_not: Optional[str],
    summary_chars: int,
    include_detail: bool,
) -> Optional[Dict[str, Any]]:
    total_start_ms = _now_ms()
    session_dir = _resolve_session_dir(logs_session_dir, session_id)
    if not session_dir:
        return None

    bundle_start_ms = _now_ms()
    bundle, bundle_cache_hit = _get_raw_timeline_bundle(session_dir=session_dir, summary_chars=summary_chars)
    bundle_prepare_ms = _elapsed_ms(bundle_start_ms)

    filter_cache_key = (
        include_non_tool,
        agent or "",
        tool or "",
        q or "",
        q_not or "",
        summary_chars,
        include_detail,
        bundle.signature,
    )
    cached_payload = _FILTERED_TIMELINE_CACHE.get((str(session_dir.resolve()), filter_cache_key))
    if cached_payload:
        payload = _copy_jsonish(cached_payload.payload)
        meta = payload.setdefault("meta", {})
        perf = meta.setdefault("perf", {})
        perf["bundle_prepare_ms"] = bundle_prepare_ms
        perf["total_ms"] = _elapsed_ms(total_start_ms)
        meta["cache"] = {
            "bundle_hit": bundle_cache_hit,
            "filtered_hit": True,
        }
        return payload

    warnings = list(bundle.warnings)
    records = bundle.records
    raw_events = bundle.raw_events
    events_filtered: List[Dict[str, Any]] = []
    agent_filter = (agent or "").strip().lower()
    tool_filter = (tool or "").strip()
    include_keywords = _parse_keyword_list(q)
    exclude_keywords = _parse_keyword_list(q_not)

    lane_id_to_label = bundle.lane_id_to_label
    filter_start_ms = _now_ms()
    turn_events_map: Dict[str, List[Dict[str, Any]]] = {}
    for ev in raw_events:
        turn_events_map.setdefault(str(ev.get("turn_ts") or ""), []).append(ev)
    turns_selected = {
        turn_ts
        for turn_ts, turn_events in turn_events_map.items()
        if _turn_passes_keyword_filter(
            turn_events=turn_events,
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
        )
    }

    for ev in raw_events:
        turn_ts = str(ev.get("turn_ts") or "")
        if turn_ts not in turns_selected:
            continue

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

        events_filtered.append(_compact_event_for_timeline(ev, include_detail))

    keyword_scope_events = [ev for ev in raw_events if str(ev.get("turn_ts") or "") in turns_selected]
    filter_ms = _elapsed_ms(filter_start_ms)

    # Build keyword-scope statistical aggregates.
    # The token usage selection follows /session/{session_id}/stats style:
    # prefer *-non-stream-res-* files when present in session, otherwise fallback to *res.json style.
    stats_start_ms = _now_ms()
    has_non_stream_files = any(bool(rec.non_stream_file) for rec in records)
    session_tokens = _new_token_bucket()
    agent_token_buckets: Dict[str, Dict[str, int]] = {}
    for rec in records:
        if rec.ts not in turns_selected:
            continue

        token_obj: Optional[Dict[str, Any]]
        token_path: Optional[str]
        if has_non_stream_files:
            token_obj = rec.non_stream_obj
            token_path = rec.non_stream_file
        else:
            token_obj = rec.non_stream_obj if rec.non_stream_file else rec.downstream_obj
            token_path = rec.non_stream_file or rec.downstream_file

        if not token_path:
            continue

        payload = token_obj if isinstance(token_obj, dict) else {}
        _apply_token_usage(session_tokens, payload, token_path)

        lane_bucket = agent_token_buckets.setdefault(rec.lane.lane_id, _new_token_bucket())
        _apply_token_usage(lane_bucket, payload, token_path)

    session_tool_counts: Dict[str, int] = {}
    agent_tool_counts: Dict[str, Dict[str, int]] = {}
    agent_tool_totals: Dict[str, int] = {}
    for ev in keyword_scope_events:
        if ev.get("kind") != "tool_call":
            continue
        lane_id = str(ev.get("lane_id") or "")
        tool_name = str(ev.get("tool_name") or "").strip() or "(unknown)"

        session_tool_counts[tool_name] = session_tool_counts.get(tool_name, 0) + 1
        lane_tool_counts = agent_tool_counts.setdefault(lane_id, {})
        lane_tool_counts[tool_name] = lane_tool_counts.get(tool_name, 0) + 1
        agent_tool_totals[lane_id] = agent_tool_totals.get(lane_id, 0) + 1

    agent_ids = set(agent_token_buckets.keys()) | set(agent_tool_totals.keys())
    filtered_turn_records = [rec for rec in records if rec.ts in turns_selected]
    duration_stats = _build_duration_stats(filtered_turn_records, lane_id_to_label)
    duration_agents_by_lane = {
        str(item.get("lane_id") or ""): item
        for item in duration_stats.get("agents", [])
        if isinstance(item, dict)
    }
    filtered_scope_agents: List[Dict[str, Any]] = []
    for lane_id in sorted(agent_ids, key=lambda x: lane_sort_key(lane_id_to_label.get(x, x))):
        duration_item = duration_agents_by_lane.get(lane_id, {})
        filtered_scope_agents.append(
            {
                "lane_id": lane_id,
                "label": lane_id_to_label.get(lane_id, lane_id),
                "tokens": agent_token_buckets.get(lane_id, _new_token_bucket()),
                "tool_calls_total": agent_tool_totals.get(lane_id, 0),
                "tool_calls_by_name": _sorted_tool_counts(agent_tool_counts.get(lane_id, {})),
                "duration": {
                    "start_ms": duration_item.get("start_ms"),
                    "end_ms": duration_item.get("end_ms"),
                    "duration_ms": int(duration_item.get("duration_ms") or 0),
                },
            }
        )

    events_filtered.sort(key=lambda x: (x.get("ts") or "", int(x.get("_seq") or 0)))
    stats_ms = _elapsed_ms(stats_start_ms)

    lanes_start_ms = _now_ms()
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
    lanes_ms = _elapsed_ms(lanes_start_ms)

    serialize_start_ms = _now_ms()
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
                detail_loaded=bool(ev.get("detail_loaded", True)),
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

    payload = {
        "session_id": session_id,
        "session_dir": bundle.session_dir_name,
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
                "detail_loaded": x.detail_loaded,
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
            "filtered_scope": {
                "turn_count_after_keywords": len(turns_selected),
                "session_tokens": session_tokens,
                "duration": duration_stats.get("session"),
                "tool_calls": {
                    "total_calls": sum(session_tool_counts.values()),
                    "by_tool": _sorted_tool_counts(session_tool_counts),
                },
                "agents": filtered_scope_agents,
            },
        },
        "meta": {
            "warnings": warnings,
            "summary_chars": summary_chars,
            "perf": {
                "bundle_prepare_ms": bundle_prepare_ms,
                "filter_ms": filter_ms,
                "stats_ms": stats_ms,
                "lanes_ms": lanes_ms,
                "serialize_ms": _elapsed_ms(serialize_start_ms),
                "total_ms": _elapsed_ms(total_start_ms),
            },
            "cache": {
                "bundle_hit": bundle_cache_hit,
                "filtered_hit": False,
            },
        },
    }
    _FILTERED_TIMELINE_CACHE[(str(session_dir.resolve()), filter_cache_key)] = _FilteredTimelineCacheEntry(
        key=filter_cache_key,
        payload=_copy_jsonish(payload),
    )
    return payload


def get_timeline_event_detail(
    *,
    logs_session_dir: str,
    session_id: str,
    event_id: str,
    summary_chars: int,
) -> Optional[Dict[str, Any]]:
    session_dir = _resolve_session_dir(logs_session_dir, session_id)
    if not session_dir:
        return None

    bundle, _cache_hit = _get_raw_timeline_bundle(session_dir=session_dir, summary_chars=summary_chars)
    for raw_event in bundle.raw_events:
        if str(raw_event.get("event_id") or "") != event_id:
            continue
        event = TimelineEvent(
            event_id=str(raw_event.get("event_id") or ""),
            ts=str(raw_event.get("ts") or ""),
            lane_id=str(raw_event.get("lane_id") or ""),
            kind=str(raw_event.get("kind") or ""),
            summary=str(raw_event.get("summary") or ""),
            detail=raw_event.get("detail"),
            detail_loaded=True,
            tool_name=raw_event.get("tool_name"),
            tool_args=raw_event.get("tool_args"),
            tool_def=raw_event.get("tool_def"),
            source_files=raw_event.get("source_files"),
            turn_ts=str(raw_event.get("turn_ts") or ""),
            format=str(raw_event.get("format") or ""),
        )
        detail_payload = {
            "event": {
                "event_id": event.event_id,
                "ts": event.ts,
                "lane_id": event.lane_id,
                "kind": event.kind,
                "summary": event.summary,
                "detail": event.detail,
                "tool_name": event.tool_name,
                "tool_args": event.tool_args,
                "tool_def": event.tool_def,
                "source_files": event.source_files,
                "turn_ts": event.turn_ts,
                "format": event.format,
                "detail_loaded": True,
            }
        }
        return detail_payload
    return None


def get_token_breakdown_for_event(
    *,
    logs_session_dir: str,
    session_id: str,
    event_id: str,
) -> Optional[Dict[str, Any]]:
    session_dir = _resolve_session_dir(logs_session_dir, session_id)
    if not session_dir:
        return None

    bundle, _ = _get_raw_timeline_bundle(session_dir=session_dir, summary_chars=120)

    # Find the turn_ts from the event_id
    turn_ts: Optional[str] = None
    for raw_event in bundle.raw_events:
        if str(raw_event.get("event_id") or "") == event_id:
            turn_ts = str(raw_event.get("turn_ts") or "")
            break

    if not turn_ts:
        return None

    # Find the turn record by turn_ts
    record: Optional[_TurnRecord] = None
    for rec in bundle.records:
        if rec.ts == turn_ts:
            record = rec
            break

    if not record:
        return None

    # Try to get the authoritative input_tokens total from API response
    total_input_tokens_from_api: Optional[int] = None
    res_obj = record.non_stream_obj or record.downstream_obj
    res_path = record.non_stream_file or record.downstream_file
    if res_obj is not None and res_path:
        in_tok, _, _ = collect_usage_tokens_for_stats(res_obj, res_path)
        if in_tok > 0:
            total_input_tokens_from_api = in_tok

    return compute_token_breakdown(
        record.req_obj,
        total_input_tokens_from_api=total_input_tokens_from_api,
        downstream_format=record.downstream_format,
    )

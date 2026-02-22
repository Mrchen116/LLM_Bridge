from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    session_dir: str
    start_ts: str
    end_ts: str
    turn_count: int
    formats: List[str]


@dataclass(frozen=True)
class Lane:
    lane_id: str
    label: str
    event_count: int
    first_ts: str
    last_ts: str


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    ts: str
    lane_id: str
    kind: str
    summary: str
    detail: Any
    turn_ts: str
    format: str
    tool_name: Optional[str] = None
    tool_args: Optional[Any] = None
    tool_def: Optional[Dict[str, Any]] = None


def session_summary_to_dict(item: SessionSummary) -> Dict[str, Any]:
    return {
        "session_id": item.session_id,
        "session_dir": item.session_dir,
        "start_ts": item.start_ts,
        "end_ts": item.end_ts,
        "turn_count": item.turn_count,
        "formats": item.formats,
    }


def lane_to_dict(item: Lane) -> Dict[str, Any]:
    return {
        "lane_id": item.lane_id,
        "label": item.label,
        "event_count": item.event_count,
        "first_ts": item.first_ts,
        "last_ts": item.last_ts,
    }


def timeline_event_to_dict(item: TimelineEvent) -> Dict[str, Any]:
    return {
        "event_id": item.event_id,
        "ts": item.ts,
        "lane_id": item.lane_id,
        "kind": item.kind,
        "summary": item.summary,
        "detail": item.detail,
        "tool_name": item.tool_name,
        "tool_args": item.tool_args,
        "tool_def": item.tool_def,
        "turn_ts": item.turn_ts,
        "format": item.format,
    }

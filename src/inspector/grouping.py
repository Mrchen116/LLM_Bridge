from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class TurnLaneInput:
    ts: str
    lane_key: str
    label_hint: str


@dataclass(frozen=True)
class AssignedLane:
    lane_id: str
    label: str


def _safe_label(label_hint: str) -> str:
    txt = " ".join((label_hint or "").split()).strip()
    if not txt:
        return "(empty)"
    if len(txt) <= 48:
        return txt
    return f"{txt[:45]}..."


def _lane_id_from_key(lane_key: str) -> str:
    return hashlib.sha1(lane_key.encode("utf-8")).hexdigest()[:12]


def assign_lanes(turns: Iterable[TurnLaneInput]) -> Dict[str, AssignedLane]:
    ordered = sorted(turns, key=lambda t: t.ts)
    lane_map: Dict[str, AssignedLane] = {}
    next_idx = 1
    for item in ordered:
        if item.lane_key in lane_map:
            continue
        lane_id = _lane_id_from_key(item.lane_key)
        label = f"Agent {next_idx} · {_safe_label(item.label_hint)}"
        lane_map[item.lane_key] = AssignedLane(lane_id=lane_id, label=label)
        next_idx += 1
    return lane_map


def lane_sort_key(label: str) -> tuple[int, str]:
    # Label format: Agent {n} · xxx
    head = label.split("·", 1)[0].strip()
    parts = head.split()
    if len(parts) == 2 and parts[0] == "Agent":
        try:
            return int(parts[1]), label
        except Exception:
            pass
    return 10**9, label

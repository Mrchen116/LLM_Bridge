from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_TS_RE = r"(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{3})"
_SESSION_DIR_RE = re.compile(rf"^{_TS_RE}_(?P<session_id>.+)$")
_REQ_FILE_RE = re.compile(rf"^{_TS_RE}-req(?:-(?P<format>[a-z_]+))?\.json$")
_NON_STREAM_FILE_RE = re.compile(rf"^{_TS_RE}-non-stream-res(?:-(?P<format>[a-z_]+))?\.json$")
_DOWNSTREAM_FILE_RE = re.compile(rf"^{_TS_RE}-downstream-res(?:-(?P<format>[a-z_]+))?\.json$")


def parse_session_dir_name(name: str) -> Optional[Tuple[str, str]]:
    m = _SESSION_DIR_RE.match(name)
    if not m:
        return None
    return m.group("ts"), m.group("session_id")


def list_session_dirs(logs_session_dir: str) -> List[Path]:
    root = Path(logs_session_dir)
    if not root.exists():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and parse_session_dir_name(p.name)]
    dirs.sort(key=lambda p: p.name, reverse=True)
    return dirs


def find_session_dirs_by_id(logs_session_dir: str, session_id: str) -> List[Path]:
    matches: List[Path] = []
    for d in list_session_dirs(logs_session_dir):
        parsed = parse_session_dir_name(d.name)
        if not parsed:
            continue
        _, sid = parsed
        if sid == session_id:
            matches.append(d)
    return matches


def list_req_files(session_dir: Path) -> List[Tuple[str, Optional[str], Path]]:
    items: List[Tuple[str, Optional[str], Path]] = []
    for fp in sorted(session_dir.glob("*-req*.json")):
        m = _REQ_FILE_RE.match(fp.name)
        if not m:
            continue
        items.append((m.group("ts"), m.group("format"), fp))
    return items


def build_turn_file_index(session_dir: Path) -> Dict[str, Dict[str, Tuple[Optional[str], Path]]]:
    index: Dict[str, Dict[str, Tuple[Optional[str], Path]]] = {}
    for fp in sorted(session_dir.glob("*.json")):
        name = fp.name
        m_req = _REQ_FILE_RE.match(name)
        if m_req:
            ts = m_req.group("ts")
            index.setdefault(ts, {})["req"] = (m_req.group("format"), fp)
            continue
        m_non = _NON_STREAM_FILE_RE.match(name)
        if m_non:
            ts = m_non.group("ts")
            index.setdefault(ts, {})["non_stream"] = (m_non.group("format"), fp)
            continue
        m_down = _DOWNSTREAM_FILE_RE.match(name)
        if m_down:
            ts = m_down.group("ts")
            index.setdefault(ts, {})["downstream"] = (m_down.group("format"), fp)
    return index


def parse_ts_to_epoch_ms(ts: str) -> int:
    dt = datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S_%f")
    return int(dt.timestamp() * 1000)

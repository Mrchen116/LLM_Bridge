from __future__ import annotations

import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
_KEEP_MIN_RAW = int(os.getenv("LOG_KEEP_MIN_RAW_PER_BUCKET", "10"))
_KEEP_MIN_SESSION = int(os.getenv("LOG_KEEP_MIN_SESSION", "50"))

_TS_LEN = 23  # YYYY-MM-DD_HH-MM-SS_msf
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{3})")

_last_cleanup_at = 0.0
_lock = threading.Lock()


def _parse_ts(name: str) -> Optional[datetime]:
    m = _TS_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S_%f")
    except ValueError:
        return None


def _cleanup_raw_logs(
    logs_raw_dir: str,
    retention_days: int,
    min_keep: int,
) -> int:
    removed = 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    root = Path(logs_raw_dir)
    if not root.exists():
        return 0

    for bucket_dir in root.iterdir():
        if not bucket_dir.is_dir():
            continue
        files: List[tuple[datetime, Path]] = []
        for fp in bucket_dir.iterdir():
            if not fp.is_file():
                continue
            ts = _parse_ts(fp.name)
            if ts is None:
                continue
            files.append((ts, fp))

        if len(files) <= min_keep:
            continue

        files.sort(key=lambda x: x[0])
        to_remove = len(files) - min_keep
        for ts, fp in files[:to_remove]:
            if ts < cutoff:
                try:
                    fp.unlink()
                    removed += 1
                except OSError:
                    pass

    return removed


def _cleanup_session_logs(
    logs_session_dir: str,
    retention_days: int,
    min_keep: int,
) -> int:
    removed = 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    root = Path(logs_session_dir)
    if not root.exists():
        return 0

    dirs: List[tuple[datetime, Path]] = []
    for dp in root.iterdir():
        if not dp.is_dir():
            continue
        ts = _parse_ts(dp.name)
        if ts is None:
            continue
        dirs.append((ts, dp))

    if len(dirs) <= min_keep:
        return 0

    dirs.sort(key=lambda x: x[0])
    to_remove = len(dirs) - min_keep
    for ts, dp in dirs[:to_remove]:
        if ts < cutoff:
            try:
                shutil.rmtree(dp, ignore_errors=True)
                removed += 1
            except OSError:
                pass

    return removed


def maybe_cleanup_logs(
    logs_raw_dir: str,
    logs_session_dir: str,
    interval_seconds: float = 3600.0,
) -> None:
    global _last_cleanup_at

    now = datetime.now(timezone.utc).timestamp()
    with _lock:
        if now - _last_cleanup_at < interval_seconds:
            return
        _last_cleanup_at = now

    def _run() -> None:
        raw_removed = _cleanup_raw_logs(logs_raw_dir, _RETENTION_DAYS, _KEEP_MIN_RAW)
        session_removed = _cleanup_session_logs(
            logs_session_dir, _RETENTION_DAYS, _KEEP_MIN_SESSION
        )
        if raw_removed or session_removed:
            print(
                f"[log-cleanup] removed {raw_removed} raw files, "
                f"{session_removed} session dirs "
                f"(older than {_RETENTION_DAYS}d, exceeding keep limits)"
            )

    threading.Thread(target=_run, daemon=True).start()

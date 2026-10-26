from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

import proxy_logging

_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "7"))
_MAX_BYTES = int(float(os.getenv("LOG_MAX_TOTAL_GB", "8")) * 1024**3)
_INTERVAL_SECONDS = 300
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{3})-")
_logger = logging.getLogger(__name__)

TurnKey = tuple[str, str, str]
_active: Counter[TurnKey] = Counter()
_lock = threading.Lock()
_request_turns: ContextVar[set[TurnKey] | None] = ContextVar("request_log_turns", default=None)


@contextmanager
def request_log_scope():
    """Protect logs until the complete HTTP response, including streaming, finishes."""
    turns: set[TurnKey] = set()
    token = _request_turns.set(turns)
    try:
        yield
    finally:
        with _lock:
            for key in turns:
                _active[key] -= 1
                if not _active[key]:
                    del _active[key]
        _request_turns.reset(token)


def track_turn(raw_dir: str, session_dir: str, timestamp: str) -> None:
    """Register a turn before its first log file is written."""
    turns = _request_turns.get()
    if turns is None:
        return
    key = (os.path.abspath(raw_dir), os.path.abspath(session_dir), timestamp)
    with _lock:
        if key not in turns:
            turns.add(key)
            _active[key] += 1


class RequestLogMiddleware:
    """Keep request protection through ASGI response completion and cancellation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not proxy_logging.REQUEST_LOGGING_ENABLED:
            return await self.app(scope, receive, send)
        with request_log_scope():
            await self.app(scope, receive, send)


def cleanup_logs(
    raw_dir: str,
    session_dir: str,
    *,
    retention_days: int = _RETENTION_DAYS,
    max_bytes: int = _MAX_BYTES,
    now: datetime | None = None,
) -> dict[str, int]:
    """Expire old turns, then evict oldest inactive turns to meet the shared cap.

    Raw and session copies of a request share its timestamp. Only timestamped
    JSON files directly inside managed bucket/session directories are eligible.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    roots = (os.path.abspath(raw_dir), os.path.abspath(session_dir))
    with _lock:
        protected = set(_active)
    groups: dict[str, list[tuple[Path, int]]] = {}
    dates: dict[str, datetime] = {}
    total = 0
    for root in roots:
        if not os.path.isdir(root) or os.path.islink(root):
            continue
        with os.scandir(root) as directories:
            for directory in directories:
                if not directory.is_dir(follow_symlinks=False):
                    continue
                with os.scandir(directory.path) as files:
                    for entry in files:
                        match = _TS_RE.match(entry.name)
                        if not match or not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                            continue
                        timestamp = match.group(1)
                        try:
                            date = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S_%f")
                            size = entry.stat(follow_symlinks=False).st_size
                        except (ValueError, FileNotFoundError):
                            continue
                        groups.setdefault(timestamp, []).append((Path(entry.path), size))
                        dates[timestamp] = date
                        total += size
    removed = 0
    freed = 0
    for timestamp in sorted(groups):
        if dates[timestamp] >= cutoff and total <= max_bytes:
            break
        key = (*roots, timestamp)
        # A request admitted during the scan may not have written all its files.
        if key in protected or dates[timestamp] >= now:
            continue
        with _lock:
            if key in _active:
                continue
            for path, size in groups[timestamp]:
                try:
                    path.unlink()
                except FileNotFoundError:
                    total -= size
                except OSError as error:
                    _logger.warning("Cannot remove log %s: %s", path, error)
                else:
                    removed += 1
                    freed += size
                    total -= size
    if removed:
        print(f"[log-cleanup] removed {removed} files, freed {freed} bytes, remaining {total} bytes", flush=True)
    return {"removed_files": removed, "freed_bytes": freed, "remaining_bytes": total}


@contextmanager
def log_cleanup_worker(raw_dir: str, session_dir: str, *, interval: float = _INTERVAL_SECONDS):
    """Run one cleanup at startup and every five minutes until service shutdown."""
    if not proxy_logging.REQUEST_LOGGING_ENABLED:
        yield
        return
    stopped = threading.Event()

    def run():
        while not stopped.is_set():
            try:
                cleanup_logs(raw_dir, session_dir)
            except OSError:
                _logger.exception("Log cleanup failed; request forwarding is unaffected")
            stopped.wait(interval)

    worker = threading.Thread(target=run, name="log-cleanup", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock, Thread
from time import time
from typing import Any, Dict, List, Optional

import zstandard

from .interfaces import ReasoningKey, ReasoningStore


_logger = logging.getLogger(__name__)
_IDLE_TTL_SECONDS = 7 * 24 * 60 * 60
_CLEANUP_INTERVAL_SECONDS = 5 * 60


class PersistentSqliteReasoningStore(ReasoningStore):
    """Keep replayable prefixes on disk and expire them after seven idle days."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._lock = Lock()
        self._db: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self._path, check_same_thread=False)
        try:
            db.execute("PRAGMA cache_size = -4096")
            # Reclaim pages from expired snapshots when their deletion commits.
            db.execute("PRAGMA auto_vacuum = FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS prefixes (
                session_id TEXT, provider TEXT, model TEXT, fingerprint TEXT, snapshot BLOB,
                last_used_at REAL NOT NULL,
                PRIMARY KEY (session_id, provider, model, fingerprint)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS prefixes_last_used ON prefixes (last_used_at)")
        except sqlite3.Error:
            db.close()
            raise
        self._db = db
        return db

    def get_decorated_prefix(self, key: ReasoningKey, fingerprint: str) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            try:
                if self._db is None and not self._path.exists():
                    return None
                db = self._connection()
                now = time()
                # Refresh and fetch in one transaction so cleanup cannot race a hit.
                with db:
                    updated = db.execute("""UPDATE prefixes SET last_used_at = ?
                        WHERE session_id=? AND provider=? AND model=? AND fingerprint=? AND last_used_at > ?""",
                        (now, *key, fingerprint, now - _IDLE_TTL_SECONDS),
                    ).rowcount
                    if not updated:
                        return None
                    row = db.execute(
                        "SELECT snapshot FROM prefixes WHERE session_id=? AND provider=? AND model=? AND fingerprint=?",
                        (*key, fingerprint),
                    ).fetchone()
            except (OSError, sqlite3.Error) as exc:
                _logger.warning("Could not read Codex reasoning cache: %s", exc)
                return None
        return json.loads(zstandard.decompress(row[0])) if row is not None else None

    def set_decorated_prefix(
        self,
        key: ReasoningKey,
        fingerprint: str,
        decorated_prefix_input: List[Dict[str, Any]],
    ) -> None:
        snapshot = zstandard.compress(json.dumps(decorated_prefix_input, separators=(",", ":")).encode(), level=1)
        with self._lock:
            try:
                db = self._connection()
                with db:
                    db.execute("""INSERT INTO prefixes VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (session_id, provider, model, fingerprint)
                        DO UPDATE SET snapshot = excluded.snapshot, last_used_at = excluded.last_used_at""",
                        (*key, fingerprint, snapshot, time()),
                    )
            except (OSError, sqlite3.Error) as exc:
                _logger.warning("Could not write Codex reasoning cache: %s", exc)

    def cleanup_expired(self) -> int:
        """Delete idle prefixes and return the number of rows removed."""
        with self._lock:
            try:
                if self._db is None and not self._path.exists():
                    return 0
                db = self._connection()
                with db:
                    return db.execute(
                        "DELETE FROM prefixes WHERE last_used_at <= ?", (time() - _IDLE_TTL_SECONDS,),
                    ).rowcount
            except (OSError, sqlite3.Error) as exc:
                _logger.warning("Could not clean up Codex reasoning cache: %s", exc)
                return 0

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @contextmanager
    def cleanup_worker(self):
        """Clean at startup and every five minutes while the proxy is running."""
        self.cleanup_expired()
        stopped = Event()

        def run():
            while not stopped.wait(_CLEANUP_INTERVAL_SECONDS):
                self.cleanup_expired()

        worker = Thread(target=run, name="reasoning-cache-cleanup", daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join()
            self.close()

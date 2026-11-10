import os
import sqlite3
from base64 import b64encode
from contextlib import closing

from src.state import sqlite_store
from src.state.sqlite_store import PersistentSqliteReasoningStore


def test_old_reasoning_prefix_is_available_after_later_snapshots_are_written(tmp_path):
    path = tmp_path / "reasoning.sqlite3"
    key = ("session", "codex_oauth", "model")
    prefix = []
    with closing(PersistentSqliteReasoningStore(path)) as store:
        for turn in range(1, 6):
            prefix.append({
                "type": "reasoning",
                "encrypted_content": b64encode(os.urandom(512 * 1024)).decode("ascii"),
            })
            store.set_decorated_prefix(key, str(turn), prefix)

    with closing(PersistentSqliteReasoningStore(path)) as reopened:
        restored = reopened.get_decorated_prefix(key, "2")
        assert restored == prefix[:2]
        restored[0]["encrypted_content"] = "changed by caller"
        assert reopened.get_decorated_prefix(key, "2") == prefix[:2]
        assert reopened.get_decorated_prefix(("other session", *key[1:]), "2") is None


def test_idle_expiration_uses_last_hit_across_restarts(tmp_path, monkeypatch):
    day = 24 * 60 * 60
    now = 0
    monkeypatch.setattr(sqlite_store, "time", lambda: now)
    path = tmp_path / "reasoning.sqlite3"
    key = ("session", "codex_oauth", "model")
    prefix = [{"type": "reasoning", "encrypted_content": "cached"}]

    with closing(PersistentSqliteReasoningStore(path)) as store:
        store.set_decorated_prefix(key, "active", prefix)
        store.set_decorated_prefix(key, "idle", prefix)
    now = 6 * day
    with closing(PersistentSqliteReasoningStore(path)) as store:
        assert store.get_decorated_prefix(key, "active") == prefix

    now = 7 * day
    store = PersistentSqliteReasoningStore(path)
    assert store.get_decorated_prefix(key, "idle") is None
    with store.cleanup_worker():
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT fingerprint FROM prefixes").fetchall() == [("active",)]

    now = 13 * day
    with closing(PersistentSqliteReasoningStore(path)) as store:
        assert store.get_decorated_prefix(key, "active") is None
        assert store.cleanup_expired() == 1

from __future__ import annotations

import json
import re
from typing import Any, Dict, Mapping, Optional


class MetadataPlugin:
    """Extract session ID from the request body's ``metadata.user_id`` field.

    Supports two encoding conventions used by Claude Code and Codex CLI:

    1. **JSON string** — ``metadata.user_id`` is a JSON object serialised as a
       string, e.g. ``'{"session_id": "abc", "uid": "..."}'``.
    2. **Legacy pattern** — ``metadata.user_id`` is a plain string containing
       ``session_<id>``, e.g. ``"user_x_session_my-session-id"``.
    """

    name = "builtin_metadata"
    priority = 200

    def extract(
        self,
        headers: Mapping[str, Any],
        body: Dict[str, Any],
    ) -> Optional[str]:
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return None
        user_id = metadata.get("user_id") or ""
        if not isinstance(user_id, str):
            return None
        stripped = user_id.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                session_id = str(parsed.get("session_id") or "").strip()
                if session_id:
                    return session_id
        m = re.search(r"session_([A-Za-z0-9-]+)", stripped)
        if m:
            return m.group(1)
        return None

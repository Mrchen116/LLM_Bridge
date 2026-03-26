from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


class HeadersPlugin:
    """Extract session ID from standard HTTP headers.

    Accepted header names (tried in order):
    - ``X-Session-Id``  — conventional header used by most clients
    - ``x-session-id``  — lower-case variant
    - ``session_id``    — Codex CLI native header
    """

    name = "builtin_headers"
    priority = 100

    def extract(
        self,
        headers: Mapping[str, Any],
        body: Dict[str, Any],
    ) -> Optional[str]:
        for key in ("X-Session-Id", "x-session-id", "session_id"):
            value = headers.get(key)
            if value is None:
                continue
            session_id = str(value).strip()
            if session_id:
                return session_id
        return None

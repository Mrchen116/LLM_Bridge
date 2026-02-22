from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.inspector.service import get_timeline, list_sessions


ROUTER = APIRouter(tags=["session-inspector"])
DEFAULT_LOGS_SESSION_DIR = os.path.join("logs", "session")
UI_DIR = Path(__file__).resolve().parents[1] / "inspector_ui"
INDEX_HTML = UI_DIR / "index.html"


def inspector_enabled() -> bool:
    return os.getenv("ENABLE_SESSION_INSPECTOR_UI", "false").lower() == "true"


def _require_enabled() -> None:
    if not inspector_enabled():
        raise HTTPException(status_code=404, detail="session inspector is disabled")


@ROUTER.get("/ui/session-inspector")
async def session_inspector_page():
    _require_enabled()
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="session inspector assets not found")
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@ROUTER.get("/api/session-inspector/sessions")
async def session_inspector_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
):
    _require_enabled()
    return list_sessions(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        limit=limit,
        cursor=cursor,
        q=q,
    )


@ROUTER.get("/api/session-inspector/sessions/{session_id}/timeline")
async def session_inspector_timeline(
    session_id: str,
    include_non_tool: bool = Query(default=True),
    agent: Optional[str] = Query(default=None),
    tool: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    summary_chars: int = Query(default=120, ge=40, le=400),
):
    _require_enabled()
    payload = get_timeline(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        session_id=session_id,
        include_non_tool=include_non_tool,
        agent=agent,
        tool=tool,
        q=q,
        summary_chars=summary_chars,
    )
    if not payload:
        raise HTTPException(status_code=404, detail=f"session_id {session_id} not found")
    return payload

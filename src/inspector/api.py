from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from src.inspector.keyword_presets import load_keyword_presets, save_keyword_presets
from src.inspector.service import (
    get_request_copy_compression_for_event,
    get_timeline,
    get_timeline_event_detail,
    get_token_breakdown_for_event,
    list_sessions,
)
from src.observability.token_stats import count_text_tokens, count_text_tokens_no_images


ROUTER = APIRouter(tags=["session-inspector"])
DEFAULT_LOGS_SESSION_DIR = os.path.join("logs", "session")
UI_DIR = Path(__file__).resolve().parents[1] / "inspector_ui"
INDEX_HTML = UI_DIR / "index.html"


def inspector_enabled() -> bool:
    return os.getenv("ENABLE_SESSION_INSPECTOR_UI", "false").lower() == "true"


def _require_enabled() -> None:
    if not inspector_enabled():
        raise HTTPException(status_code=404, detail="session inspector is disabled")


def _workspace_relative(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _resolve_log_file_path(path_value: str) -> Path:
    raw = (path_value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="path is required")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()

    logs_root = (Path.cwd() / DEFAULT_LOGS_SESSION_DIR).resolve()
    try:
        candidate.relative_to(logs_root)
    except Exception:
        raise HTTPException(status_code=400, detail="path must be under logs/session")

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="log file not found")
    return candidate


@ROUTER.get("/ui/session-inspector")
async def session_inspector_page():
    _require_enabled()
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="session inspector assets not found")
    return FileResponse(str(INDEX_HTML), media_type="text/html")


@ROUTER.get("/api/session-inspector/sessions")
async def session_inspector_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    q: Optional[str] = Query(default=None),
):
    _require_enabled()
    return list_sessions(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        limit=limit,
        page=page,
        q=q,
    )


@ROUTER.get("/api/session-inspector/sessions/{session_id}/timeline")
async def session_inspector_timeline(
    session_id: str,
    include_non_tool: bool = Query(default=True),
    agent: Optional[str] = Query(default=None),
    tool: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    q_not: Optional[str] = Query(default=None),
    summary_chars: int = Query(default=120, ge=40, le=400),
    include_detail: bool = Query(default=True),
):
    _require_enabled()
    payload = get_timeline(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        session_id=session_id,
        include_non_tool=include_non_tool,
        agent=agent,
        tool=tool,
        q=q,
        q_not=q_not,
        summary_chars=summary_chars,
        include_detail=include_detail,
    )
    if not payload:
        raise HTTPException(status_code=404, detail=f"session_id {session_id} not found")
    return payload


@ROUTER.get("/api/session-inspector/sessions/{session_id}/events/{event_id}")
async def session_inspector_event_detail(
    session_id: str,
    event_id: str,
    summary_chars: int = Query(default=120, ge=40, le=400),
):
    _require_enabled()
    payload = get_timeline_event_detail(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        session_id=session_id,
        event_id=event_id,
        summary_chars=summary_chars,
    )
    if not payload:
        raise HTTPException(status_code=404, detail=f"event_id {event_id} not found")
    return payload


@ROUTER.get("/api/session-inspector/sessions/{session_id}/events/{event_id}/token-breakdown")
async def session_inspector_token_breakdown(
    session_id: str,
    event_id: str,
):
    _require_enabled()
    payload = get_token_breakdown_for_event(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        session_id=session_id,
        event_id=event_id,
    )
    if not payload:
        raise HTTPException(status_code=404, detail=f"event_id {event_id} not found")
    return payload


@ROUTER.get("/api/session-inspector/sessions/{session_id}/events/{event_id}/request-copy-compression")
async def session_inspector_request_copy_compression(
    session_id: str,
    event_id: str,
    threshold_tokens: int = Query(default=500, ge=1, le=100000),
):
    _require_enabled()
    payload = get_request_copy_compression_for_event(
        logs_session_dir=DEFAULT_LOGS_SESSION_DIR,
        session_id=session_id,
        event_id=event_id,
        threshold_tokens=threshold_tokens,
    )
    if not payload:
        raise HTTPException(status_code=404, detail=f"event_id {event_id} not found")
    return payload


@ROUTER.get("/api/session-inspector/log-file")
async def session_inspector_log_file(path: str = Query(...)):
    _require_enabled()
    log_path = _resolve_log_file_path(path)
    content = log_path.read_text(encoding="utf-8", errors="replace")

    return {
        "path": _workspace_relative(log_path),
        "content": content,
        "size_bytes": log_path.stat().st_size,
        "truncated": False,
    }


@ROUTER.get("/api/session-inspector/keyword-presets")
async def session_inspector_keyword_presets():
    _require_enabled()
    return load_keyword_presets()


@ROUTER.put("/api/session-inspector/keyword-presets")
async def session_inspector_keyword_presets_update(payload: dict):
    _require_enabled()
    return save_keyword_presets(payload)


@ROUTER.post("/api/session-inspector/text-token-count")
async def session_inspector_text_token_count(payload: dict):
    _require_enabled()
    return {"tokens": count_text_tokens_no_images(payload.get("text"))}

import os
import httpx

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from upstream_config import load_and_validate_config
load_dotenv(override=True)
from proxy_logging import _dump_json as _proxy_dump_json

from src.handlers.chat_completions import run_chat_completions_flow
from src.handlers.messages import run_messages_flow
from src.handlers.responses import run_responses_flow
from src.inspector.api import ROUTER as SESSION_INSPECTOR_ROUTER, UI_DIR as SESSION_INSPECTOR_UI_DIR
from src.observability.session_metrics import get_session_stats
from src.observability.token_stats import (
    TOKEN_FORMAT_ANTHROPIC,
    collect_usage_tokens_for_stats,
    count_input_tokens_for_request,
)

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
ENABLE_SESSION_INSPECTOR_UI = os.getenv("ENABLE_SESSION_INSPECTOR_UI", "false").lower() == "true"
UPSTREAM_CONFIG = load_and_validate_config()

LOGS_ROOT_DIR = "logs"
LOGS_RAW_DIR = os.path.join(LOGS_ROOT_DIR, "raw")
LOGS_SESSION_DIR = os.path.join(LOGS_ROOT_DIR, "session")
LOGS_CODEAGENT_DIR = os.path.join(LOGS_ROOT_DIR, "codeagent")


app = FastAPI(title="Anthropic+OpenAI Proxy (FastAPI)")
app.include_router(SESSION_INSPECTOR_ROUTER)
app.mount(
    "/ui/session-inspector/assets",
    StaticFiles(directory=str(SESSION_INSPECTOR_UI_DIR)),
    name="session_inspector_assets",
)

# Backward-compat test hooks
_dump_json = _proxy_dump_json


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/responses")
async def openai_responses(req: Request):
    return await run_responses_flow(
        req,
        ban_explore=BAN_EXPLORE,
        upstream_config=UPSTREAM_CONFIG,
        logs_raw_dir=LOGS_RAW_DIR,
        logs_session_dir=LOGS_SESSION_DIR,
    )


@app.post("/v1/messages")
async def v1_messages(req: Request):
    return await run_messages_flow(
        req,
        ban_stream=BAN_STREAM,
        ban_explore=BAN_EXPLORE,
        expose_thinking=EXPOSE_THINKING,
        upstream_config=UPSTREAM_CONFIG,
        logs_raw_dir=LOGS_RAW_DIR,
        logs_session_dir=LOGS_SESSION_DIR,
    )
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}, status_code=400)
    fmt, token_count = count_input_tokens_for_request(body, default_format=TOKEN_FORMAT_ANTHROPIC)
    return {"format": fmt, "input_tokens": token_count}


@app.get("/session/{session_id}/stats")
async def session_stats(session_id: str):
    stats = get_session_stats(
        session_id=session_id,
        logs_session_dir=LOGS_SESSION_DIR,
        logs_codeagent_dir=LOGS_CODEAGENT_DIR,
        collect_usage_tokens=collect_usage_tokens_for_stats,
    )
    if not stats:
        return JSONResponse(
            {"error": f"session_id {session_id} not found"},
            status_code=404,
        )

    return {
        "session_id": session_id,
        "input_tokens": stats.get("input_tokens", 0),
        "output_tokens": stats.get("output_tokens", 0),
        "num_turns": stats.get("num_turns", 0),
        "by_format": stats.get("by_format", {}),
    }


# ---------- OpenAI Chat Completions ----------
@app.post("/v1/chat/completions")
async def openai_chat_completions(req: Request):
    return await run_chat_completions_flow(
        req,
        ban_explore=BAN_EXPLORE,
        upstream_config=UPSTREAM_CONFIG,
        logs_raw_dir=LOGS_RAW_DIR,
        logs_session_dir=LOGS_SESSION_DIR,
    )

import os
import httpx

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from upstream_config import load_and_validate_config
load_dotenv(override=True)
from proxy_converters import calculate_token_count
from proxy_logging import _collect_usage_tokens, _dump_json as _proxy_dump_json

from src.handlers.chat_completions import run_chat_completions_flow
from src.handlers.messages import run_messages_flow
from src.handlers.responses import run_responses_flow
from src.observability.session_metrics import get_session_stats

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
UPSTREAM_CONFIG = load_and_validate_config()

LOGS_ROOT_DIR = "logs"
LOGS_OPENAI_DIR = os.path.join(LOGS_ROOT_DIR, "openai")
LOGS_ANTHROPIC_DIR = os.path.join(LOGS_ROOT_DIR, "anthropic")
LOGS_SESSION_DIR = os.path.join(LOGS_ROOT_DIR, "session")
LOGS_CODEAGENT_DIR = os.path.join(LOGS_ROOT_DIR, "codeagent")


app = FastAPI(title="Anthropic+OpenAI Proxy (FastAPI)")

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
        logs_openai_dir=LOGS_OPENAI_DIR,
    )


@app.post("/v1/messages")
async def v1_messages(req: Request):
    return await run_messages_flow(
        req,
        ban_stream=BAN_STREAM,
        ban_explore=BAN_EXPLORE,
        expose_thinking=EXPOSE_THINKING,
        upstream_config=UPSTREAM_CONFIG,
        logs_anthropic_dir=LOGS_ANTHROPIC_DIR,
        logs_session_dir=LOGS_SESSION_DIR,
    )
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}, status_code=400)

    messages = body.get("messages", [])
    system = body.get("system")
    tools = body.get("tools")

    token_count = calculate_token_count(messages, system, tools)

    return {"input_tokens": token_count}


@app.get("/session/{session_id}/stats")
async def session_stats(session_id: str):
    stats = get_session_stats(
        session_id=session_id,
        logs_session_dir=LOGS_SESSION_DIR,
        logs_codeagent_dir=LOGS_CODEAGENT_DIR,
        collect_usage_tokens=_collect_usage_tokens,
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
    }


# ---------- OpenAI Chat Completions ----------
@app.post("/v1/chat/completions")
async def openai_chat_completions(req: Request):
    return await run_chat_completions_flow(
        req,
        ban_explore=BAN_EXPLORE,
        upstream_config=UPSTREAM_CONFIG,
        logs_openai_dir=LOGS_OPENAI_DIR,
        logs_codeagent_dir=LOGS_CODEAGENT_DIR,
    )

import os
import httpx
from types import SimpleNamespace

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from upstream_config import load_and_validate_config
load_dotenv(override=True)
import proxy_converters as converters_mod
import proxy_logging as proxy_logging_mod

import src.orchestrator.reasoning_reinject as reasoning_mod
import src.adapters.upstream_executor as executor_mod
from src.ingress.chat_completions import handle_openai_chat_completions
from src.ingress.messages import handle_v1_messages
from src.ingress.responses import handle_openai_responses
from src.observability.session_metrics import get_session_stats
from src.runtime.context import RuntimeContext

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
_dump_json = proxy_logging_mod._dump_json


def _build_proxy_logging_context() -> SimpleNamespace:
    return SimpleNamespace(
        _build_anthropic_non_stream_from_events=proxy_logging_mod._build_anthropic_non_stream_from_events,
        _collect_usage_tokens=proxy_logging_mod._collect_usage_tokens,
        _discard_session_req=proxy_logging_mod._discard_session_req,
        _dump_json=_dump_json,
        _extract_usage_from_obj=proxy_logging_mod._extract_usage_from_obj,
        _parse_anthropic_sse_chunks_to_events=proxy_logging_mod._parse_anthropic_sse_chunks_to_events,
        _resp_to_obj=proxy_logging_mod._resp_to_obj,
        _should_skip_session_logging=proxy_logging_mod._should_skip_session_logging,
        _sse_event=proxy_logging_mod._sse_event,
        _usage_dict_has_tokens=proxy_logging_mod._usage_dict_has_tokens,
    )


def _build_runtime_context() -> RuntimeContext:
    return RuntimeContext(
        ban_explore=BAN_EXPLORE,
        ban_stream=BAN_STREAM,
        expose_thinking=EXPOSE_THINKING,
        upstream_config=UPSTREAM_CONFIG,
        logs_openai_dir=LOGS_OPENAI_DIR,
        logs_anthropic_dir=LOGS_ANTHROPIC_DIR,
        logs_session_dir=LOGS_SESSION_DIR,
        logs_codeagent_dir=LOGS_CODEAGENT_DIR,
        converters=converters_mod,
        proxy_logging=_build_proxy_logging_context(),
        reasoning=reasoning_mod,
        executor=executor_mod,
    )


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/responses")
async def openai_responses(req: Request):
    return await handle_openai_responses(req, _build_runtime_context())


@app.post("/v1/messages")
async def v1_messages(req: Request):
    return await handle_v1_messages(req, _build_runtime_context())
@app.post("/v1/messages/count_tokens")
async def v1_messages_count_tokens(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}, status_code=400)

    messages = body.get("messages", [])
    system = body.get("system")
    tools = body.get("tools")

    token_count = converters_mod.calculate_token_count(messages, system, tools)

    return {"input_tokens": token_count}


@app.get("/session/{session_id}/stats")
async def session_stats(session_id: str):
    stats = get_session_stats(
        session_id=session_id,
        logs_session_dir=LOGS_SESSION_DIR,
        logs_codeagent_dir=LOGS_CODEAGENT_DIR,
        collect_usage_tokens=proxy_logging_mod._collect_usage_tokens,
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
    return await handle_openai_chat_completions(req, _build_runtime_context())

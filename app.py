import os
import json
import time
import uuid
import asyncio
import glob
from typing import Any, Dict, List, Optional, AsyncIterator, Tuple

import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_upstream_url,
    get_runtime_options,
    get_effective_auth_type,
    load_and_validate_config,
    resolve_profile,
)
load_dotenv(override=True)
import logging
import src.transforms.gateway_transforms as converters_mod
import src.observability.gateway_logging as proxy_logging_mod

from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _codex_responses_to_chat_completion,
    _extract_codex_output_tool_uses,
    _extract_model_and_ban_explore,
    _strip_task_explore_line,
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai_tools,
    calculate_token_count,
    oai_finish_reason_to_stop_reason,
)
from proxy_logging import (
    _build_anthropic_non_stream_from_events,
    _collect_usage_tokens,
    _discard_session_req,
    _dump_json,
    _extract_usage_from_obj,
    _parse_anthropic_sse_chunks_to_events,
    _resp_to_obj,
    _should_skip_session_logging,
    _sse_event,
    _usage_dict_has_tokens,
)
from src.orchestrator.reasoning_reinject import (
    _extract_response_completed_object_from_sse_chunks,
    _extract_session_id_from_body_metadata,
    _maybe_reinject_codex_reasoning,
    _maybe_reinject_codex_reasoning_for_responses,
    _update_codex_reasoning_reinject_cache,
    _update_codex_reasoning_reinject_cache_for_responses,
)
import src.orchestrator.reasoning_reinject as reasoning_mod
from src.adapters.upstream_executor import (
    build_headers_by_profile as _build_headers_by_profile,
    collect_codex_response_from_stream as _collect_codex_response_from_stream,
    is_rate_limit_status,
)
import src.adapters.upstream_executor as executor_mod
from src.ingress.chat_completions import handle_openai_chat_completions
from src.ingress.messages import handle_v1_messages
from src.ingress.responses import handle_openai_responses
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
        proxy_logging=proxy_logging_mod,
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

    token_count = calculate_token_count(messages, system, tools)

    return {"input_tokens": token_count}


@app.get("/session/{session_id}/stats")
async def session_stats(session_id: str):
    """
    返回指定 session_id 的 token 统计：
    - input_tokens：所有 *res.json 的 input_tokens 之和
    - output_tokens：所有 *res.json 的 output_tokens 之和
    - num_turns：*res.json 文件数
    """
    def _scan_session_dirs(session_dirs: List[str]) -> Dict[str, int]:
        total_input = 0
        total_output = 0
        num_turns = 0
        for d in session_dirs:
            for fp in glob.glob(os.path.join(d, "*res.json")):
                num_turns += 1
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    continue
                in_tok, out_tok = _collect_usage_tokens(data)
                total_input += in_tok
                total_output += out_tok
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "num_turns": num_turns,
        }

    session_dirs = sorted(glob.glob(os.path.join(LOGS_SESSION_DIR, f"*_{session_id}")))
    stats = _scan_session_dirs(session_dirs) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        # session 日志无数据时再回退到 codeagent 日志
        session_dirs = sorted(glob.glob(os.path.join(LOGS_CODEAGENT_DIR, f"*_{session_id}")))
        stats = _scan_session_dirs(session_dirs) if session_dirs else None

    if not stats or stats.get("num_turns", 0) == 0:
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

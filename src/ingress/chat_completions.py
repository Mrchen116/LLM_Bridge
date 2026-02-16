from __future__ import annotations

from fastapi import Request

from src.orchestrator.chat_flow import run_chat_completions_flow
from src.runtime.context import RuntimeContext


async def handle_openai_chat_completions(req: Request, ctx: RuntimeContext):
    return await run_chat_completions_flow(req, ctx)

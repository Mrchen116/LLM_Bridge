from __future__ import annotations

from fastapi import Request
from src.runtime.context import RuntimeContext
from src.orchestrator.responses_flow import run_responses_flow


async def handle_openai_responses(req: Request, ctx: RuntimeContext):
    return await run_responses_flow(req, ctx)

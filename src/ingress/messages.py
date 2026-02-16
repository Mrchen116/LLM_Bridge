from __future__ import annotations

from fastapi import Request

from src.orchestrator.messages_flow import run_messages_flow
from src.runtime.context import RuntimeContext


async def handle_v1_messages(req: Request, ctx: RuntimeContext):
    return await run_messages_flow(req, ctx)

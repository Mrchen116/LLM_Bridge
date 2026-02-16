from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

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


def extract_model_and_ban_explore(raw_model: Any, ban_explore: bool) -> Tuple[Optional[str], bool]:
    return _extract_model_and_ban_explore(raw_model, ban_explore)


def strip_task_explore_line(tools: Optional[List[Dict[str, Any]]], ban_explore: Optional[bool] = None):
    return _strip_task_explore_line(tools, ban_explore=ban_explore)


# Backward-compat aliases used by existing flows during migration
_extract_model_and_ban_explore = _extract_model_and_ban_explore
_strip_task_explore_line = _strip_task_explore_line
_build_codex_responses_payload_from_chat = _build_codex_responses_payload_from_chat
_codex_responses_to_chat_completion = _codex_responses_to_chat_completion
_extract_codex_output_tool_uses = _extract_codex_output_tool_uses

from __future__ import annotations

from typing import Any, Dict, List, Optional

from proxy_converters import (
    _build_codex_responses_payload_from_chat,
    _codex_responses_to_chat_completion,
    _extract_codex_output_tool_uses,
)


def openai_chat_body_to_codex_payload(
    body: Dict[str, Any], model: str, *, model_suffix_effort: Optional[str] = None
) -> Dict[str, Any]:
    return _build_codex_responses_payload_from_chat(body, model, model_suffix_effort=model_suffix_effort)


def codex_response_to_openai_chat_completion(resp_json: Dict[str, Any], model: str) -> Dict[str, Any]:
    return _codex_responses_to_chat_completion(resp_json, model)


def codex_response_extract_tool_uses(resp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _extract_codex_output_tool_uses(resp_json)


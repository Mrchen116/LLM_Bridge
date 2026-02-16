from __future__ import annotations

from typing import Any, Dict, List, Optional

from proxy_converters import (
    anthropic_messages_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai_tools,
    oai_finish_reason_to_stop_reason,
)


def anthropic_messages_to_openai_chat_messages(messages: List[Dict[str, Any]], system: Any) -> List[Dict[str, Any]]:
    return anthropic_messages_to_openai(messages, system)


def anthropic_tools_to_openai_chat_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    return anthropic_tools_to_openai_tools(tools)


def anthropic_tool_choice_to_openai_chat_tool_choice(tool_choice: Optional[Dict[str, Any]]) -> Optional[Any]:
    return anthropic_tool_choice_to_openai(tool_choice)


def openai_chat_finish_reason_to_anthropic_stop_reason(finish_reason: Optional[str]) -> Optional[str]:
    return oai_finish_reason_to_stop_reason(finish_reason)


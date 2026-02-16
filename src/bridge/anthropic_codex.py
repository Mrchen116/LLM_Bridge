from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.bridge.anthropic_openai import (
    anthropic_messages_to_openai_chat_messages,
    anthropic_tool_choice_to_openai_chat_tool_choice,
    anthropic_tools_to_openai_chat_tools,
)
from src.bridge.openai_codex import openai_chat_body_to_codex_payload


def anthropic_request_to_openai_chat_body(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    system: Any,
    max_tokens: int,
    stream: bool,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages_to_openai_chat_messages(messages, system),
        "max_tokens": max_tokens,
        "stream": stream,
    }
    mapped_tools = anthropic_tools_to_openai_chat_tools(tools)
    if mapped_tools:
        payload["tools"] = mapped_tools
    mapped_tool_choice = anthropic_tool_choice_to_openai_chat_tool_choice(tool_choice)
    if mapped_tool_choice is not None:
        payload["tool_choice"] = mapped_tool_choice
    if isinstance(tool_choice, dict) and "disable_parallel_tool_use" in tool_choice:
        payload["parallel_tool_calls"] = (not bool(tool_choice["disable_parallel_tool_use"]))
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if stop_sequences is not None:
        payload["stop"] = stop_sequences
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def anthropic_request_to_codex_payload(
    *,
    model: str,
    messages: List[Dict[str, Any]],
    system: Any,
    max_tokens: int,
    stream: bool,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    stop_sequences: Optional[List[str]] = None,
) -> Dict[str, Any]:
    openai_chat_body = anthropic_request_to_openai_chat_body(
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        stream=stream,
        tools=tools,
        tool_choice=tool_choice,
        temperature=temperature,
        top_p=top_p,
        stop_sequences=stop_sequences,
    )
    return openai_chat_body_to_codex_payload(openai_chat_body, model)


import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import tiktoken

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"

# 是否暴露 reasoning_content 到 anthropic thinking block
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"

# 初始化 Claude 使用的编码器 (延迟加载)
_enc = None


def calculate_token_count(messages: List[Dict[str, Any]], system: Any, tools: Optional[List[Dict[str, Any]]]) -> int:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("cl100k_base")

    token_count = 0

    # 计算 messages
    if isinstance(messages, list):
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                token_count += len(_enc.encode(content))
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    p_type = part.get("type")
                    if p_type == "text":
                        token_count += len(_enc.encode(part.get("text", "")))
                    elif p_type == "tool_use":
                        token_count += len(_enc.encode(json.dumps(part.get("input", {}), ensure_ascii=False)))
                    elif p_type == "tool_result":
                        tool_content = part.get("content")
                        if isinstance(tool_content, str):
                            token_count += len(_enc.encode(tool_content))
                        else:
                            token_count += len(_enc.encode(json.dumps(tool_content, ensure_ascii=False)))

    # 计算 system
    if isinstance(system, str):
        token_count += len(_enc.encode(system))
    elif isinstance(system, list):
        for s in system:
            if isinstance(s, dict) and s.get("type") == "text":
                token_count += len(_enc.encode(s.get("text", "")))
            elif isinstance(s, str):
                token_count += len(_enc.encode(s))

    # 计算 tools
    if tools:
        for t in tools:
            name = t.get("name", "")
            description = t.get("description", "")
            token_count += len(_enc.encode(name + description))
            input_schema = t.get("input_schema")
            if input_schema:
                token_count += len(_enc.encode(json.dumps(input_schema, ensure_ascii=False)))

    return token_count


def _extract_text_from_blocks(blocks: Any) -> str:
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return blocks
    if isinstance(blocks, list):
        parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "".join(parts)
    return str(blocks)


def anthropic_messages_to_openai(messages: List[Dict[str, Any]], system: Any) -> List[Dict[str, Any]]:
    """
    Anthropic Messages -> OpenAI ChatCompletions
    - user text -> {"role":"user","content": "..."}
    - assistant tool_use blocks -> assistant message with tool_calls
    - user tool_result blocks -> {"role":"tool","tool_call_id": "...","content":"..."}
    """
    out: List[Dict[str, Any]] = []

    sys_text = _extract_text_from_blocks(system)
    if sys_text.strip():
        out.append({"role": "system", "content": sys_text})

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")

        # content 可能是 string 或 blocks[]
        if isinstance(content, str) or content is None:
            text = _extract_text_from_blocks(content)
            if text.strip() or role != "tool":
                out.append({"role": role if role in ("user", "assistant", "system") else "user", "content": text})
            continue

        if not isinstance(content, list):
            text = _extract_text_from_blocks(content)
            out.append({"role": role if role in ("user", "assistant", "system") else "user", "content": text})
            continue

        if role == "assistant":
            text_parts: List[str] = []
            thinking_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "thinking":
                    thinking_parts.append(b.get("thinking", ""))
                elif t == "tool_use":
                    tool_id = b.get("id")
                    name = b.get("name")
                    tool_input = b.get("input", {})
                    if tool_id and name:
                        tool_calls.append(
                            {
                                "id": tool_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(tool_input, ensure_ascii=False),
                                },
                            }
                        )

            msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            if thinking_parts:
                msg["reasoning_content"] = "".join(thinking_parts)
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user":
            text_parts: List[str] = []
            tool_results: List[Dict[str, Any]] = []

            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "tool_result":
                    tool_use_id = b.get("tool_use_id")
                    tool_content = b.get("content")
                    tool_text = _extract_text_from_blocks(tool_content)
                    if tool_use_id:
                        tool_results.append({"tool_call_id": tool_use_id, "content": tool_text})

            user_text = "".join(text_parts)
            if user_text.strip():
                out.append({"role": "user", "content": user_text})
            elif not tool_results:
                out.append({"role": "user", "content": ""})

            for tr in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"],
                    }
                )
            continue

        out.append({"role": "user", "content": _extract_text_from_blocks(content)})

    return out


def anthropic_tools_to_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    if not tools:
        return None

    out = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out if out else None


def _strip_task_explore_line(
    tools: Optional[List[Dict[str, Any]]],
    ban_explore: Optional[bool] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    根据 ban_explore 决定是否从 Task 工具描述中移除 "- Explore:" 行。
    - ban_explore 为 None 时，采用全局 BAN_EXPLORE 开关。
    """

    def _remove_explore_from_desc(desc: Any) -> Optional[str]:
        if not isinstance(desc, str):
            return None
        lines = desc.splitlines()
        filtered_lines = []
        changed = False
        for line in lines:
            if line.lstrip().startswith("- Explore:") or line.lstrip().startswith("- **Explore**:"):
                changed = True
                continue
            filtered_lines.append(line)
        return "\n".join(filtered_lines) if changed else None

    if ban_explore is None:
        ban_explore = BAN_EXPLORE

    if not ban_explore or not tools:
        return tools

    cleaned: List[Any] = []
    for t in tools:
        if not isinstance(t, dict):
            cleaned.append(t)
            continue
        if t.get("name") == "Task":
            new_desc = _remove_explore_from_desc(t.get("description"))
            if new_desc is not None:
                cleaned.append({**t, "description": new_desc})
            else:
                cleaned.append(t)
            continue

        if t.get("type") == "function":
            func = t.get("function")
            if isinstance(func, dict) and func.get("name") == "Task":
                new_desc = _remove_explore_from_desc(func.get("description"))
                if new_desc is not None:
                    cleaned.append({**t, "function": {**func, "description": new_desc}})
                else:
                    cleaned.append(t)
                continue

        cleaned.append(t)

    return cleaned


def anthropic_tool_choice_to_openai(tool_choice: Optional[Dict[str, Any]]) -> Optional[Any]:
    if not tool_choice:
        return None

    t = tool_choice.get("type")
    if t == "auto":
        return "auto"
    if t == "none":
        return "none"
    if t == "tool":
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
        return "auto"
    if t == "any":
        return "auto"
    return "auto"


def oai_finish_reason_to_stop_reason(fr: Optional[str]) -> Optional[str]:
    if fr in (None, ""):
        return None
    if fr == "length":
        return "max_tokens"
    if fr == "stop":
        return "end_turn"
    if fr == "tool_calls":
        return "tool_use"
    if fr == "content_filter":
        return "stop_sequence"
    return "end_turn"


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


def _tool_part_to_responses_output_part(part: Any) -> Optional[Dict[str, Any]]:
    if isinstance(part, str):
        return {"type": "input_text", "text": part} if part else None
    if not isinstance(part, dict):
        return None

    p_type = str(part.get("type") or "")
    if p_type in {"text", "input_text", "output_text"}:
        text = str(part.get("text") or "")
        return {"type": "input_text", "text": text} if text else None

    if p_type in {"image_url", "input_image"} or "image_url" in part:
        image_url = part.get("image_url")
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        else:
            url = str(image_url or "")
        if url:
            return {"type": "input_image", "image_url": url}
        return None

    if "text" in part:
        text = str(part.get("text") or "")
        return {"type": "input_text", "text": text} if text else None

    return None


def _normalize_tool_content_parts(parts: Any) -> List[Dict[str, Any]]:
    if not isinstance(parts, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in parts:
        converted = _tool_part_to_responses_output_part(item)
        if converted is not None:
            out.append(converted)
            continue
        if isinstance(item, dict):
            fallback = _message_content_to_text(item)
            if fallback:
                out.append({"type": "input_text", "text": fallback})
            continue
        if item is not None:
            out.append({"type": "input_text", "text": str(item)})
    return out


def _extract_structured_tool_output(content: Any) -> Optional[List[Dict[str, Any]]]:
    # 兼容常见形态：
    # 1) tool.content 直接是 blocks 数组
    # 2) tool.content 是 {"content":[...]} 或 {"output":{"content":[...]}}
    if isinstance(content, list):
        normalized = _normalize_tool_content_parts(content)
        return normalized if normalized else None

    if isinstance(content, dict):
        if isinstance(content.get("content"), list):
            normalized = _normalize_tool_content_parts(content.get("content"))
            if normalized:
                return normalized
        output = content.get("output")
        if isinstance(output, dict) and isinstance(output.get("content"), list):
            normalized = _normalize_tool_content_parts(output.get("content"))
            if normalized:
                return normalized
        if isinstance(output, list):
            normalized = _normalize_tool_content_parts(output)
            if normalized:
                return normalized
        single = _tool_part_to_responses_output_part(content)
        if single is not None:
            return [single]
        return None

    return None


def _tool_content_to_function_output(content: Any) -> Any:
    if content is None:
        return ""

    if isinstance(content, str):
        stripped = content.strip()
        if stripped and stripped[0] in "[{":
            try:
                decoded = json.loads(stripped)
                structured = _extract_structured_tool_output(decoded)
                if structured is not None:
                    return structured
            except Exception:
                pass
        return content

    structured = _extract_structured_tool_output(content)
    if structured is not None:
        return structured

    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)

    return str(content)


def _chat_tool_choice_to_responses(tool_choice: Any) -> Any:
    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "none", "required"}:
            return tool_choice
        return "auto"
    if not isinstance(tool_choice, dict):
        return None

    t = str(tool_choice.get("type") or "")
    if t in {"auto", "none", "required"}:
        return t
    if t == "function":
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            name = str(fn.get("name") or "")
            if name:
                return {"type": "function", "name": name}
    return "auto"


def _chat_tools_to_responses_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tools, list):
        return None

    out: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "") != "function":
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        item: Dict[str, Any] = {
            "type": "function",
            "name": name,
        }
        if fn.get("description") is not None:
            item["description"] = fn.get("description")
        if fn.get("parameters") is not None:
            item["parameters"] = fn.get("parameters")
        out.append(item)
    return out


def _chat_content_to_responses_input_parts(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        text = _message_content_to_text(content)
        return [{"type": "input_text", "text": text}] if text else []

    out: List[Dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                out.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        p_type = str(part.get("type") or "")
        if p_type == "text":
            text = str(part.get("text") or "")
            if text:
                out.append({"type": "input_text", "text": text})
            continue
        if p_type == "image_url" and isinstance(part.get("image_url"), dict):
            url = str(part["image_url"].get("url") or "")
            if url:
                out.append({"type": "input_image", "image_url": url})
            continue
        fallback = _message_content_to_text(part)
        if fallback:
            out.append({"type": "input_text", "text": fallback})
    return out


def _normalize_reasoning_effort(value: Any) -> Optional[str]:
    if value is None:
        return None
    effort = str(value).strip().lower()
    # Responses API 的推理强度白名单，避免把无效值透传到上游导致 4xx。
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    if effort in allowed:
        return effort
    return None


def anthropic_output_config_to_codex_reasoning_effort(output_config: Any) -> Optional[str]:
    """Map Anthropic Messages output_config.effort to Codex Responses reasoning.effort."""
    if not isinstance(output_config, dict):
        return None

    raw_effort = output_config.get("effort")
    if raw_effort is None:
        return None

    return _normalize_reasoning_effort(raw_effort)


def resolve_codex_upstream_reasoning_effort(
    *,
    reasoning_effort_top: Any = None,
    reasoning_dict: Optional[Dict[str, Any]] = None,
    model_suffix_effort: Optional[str] = None,
) -> str:
    """解析发往 Codex 的 reasoning.effort（全下游统一）：显式 body > 模型名 @后缀 > 默认 medium。"""
    effort: Optional[str] = None
    if isinstance(reasoning_dict, dict) and reasoning_dict.get("effort") is not None:
        effort = _normalize_reasoning_effort(reasoning_dict.get("effort"))
    if effort is None:
        effort = _normalize_reasoning_effort(reasoning_effort_top)
    if effort is None:
        effort = _normalize_reasoning_effort(model_suffix_effort)
    if effort is None:
        return "medium"
    return effort


def ensure_codex_responses_include_encrypted_reasoning(
    payload: Dict[str, Any], *, include_source: Optional[Dict[str, Any]] = None
) -> None:
    """为无状态 Codex Responses 补齐 include：保证含 reasoning.encrypted_content（与用户已有 include 并集）。"""
    src = payload if include_source is None else include_source
    include_items: List[str] = []
    raw_include = src.get("include")
    if isinstance(raw_include, list):
        include_items = [str(x) for x in raw_include if x is not None]
    elif isinstance(raw_include, str) and raw_include.strip():
        include_items = [raw_include.strip()]
    if "reasoning.encrypted_content" not in include_items:
        include_items.append("reasoning.encrypted_content")
    payload["include"] = include_items


def _build_codex_responses_payload_from_chat(
    body: Dict[str, Any], model: str, *, model_suffix_effort: Optional[str] = None
) -> Dict[str, Any]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    input_items: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")

        if role in {"system", "developer"}:
            text = _message_content_to_text(content)
            if text:
                input_items.append({"role": "developer" if role == "developer" else "system", "content": text})
            continue

        if role == "user":
            parts = _chat_content_to_responses_input_parts(content)
            if parts:
                input_items.append({"role": "user", "content": parts})
            continue

        if role == "assistant":
            text = _message_content_to_text(content)
            if text:
                input_items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            tool_calls = m.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    if str(tc.get("type") or "") != "function":
                        continue
                    tc_id = str(tc.get("id") or "")
                    fn = tc.get("function")
                    if not tc_id or not isinstance(fn, dict):
                        continue
                    name = str(fn.get("name") or "")
                    arguments = fn.get("arguments")
                    if not name:
                        continue
                    if isinstance(arguments, str):
                        args_str = arguments
                    elif arguments is None:
                        args_str = "{}"
                    else:
                        args_str = json.dumps(arguments, ensure_ascii=False)
                    input_items.append(
                        {"type": "function_call", "call_id": tc_id, "name": name, "arguments": args_str}
                    )
            continue

        if role == "tool":
            call_id = str(m.get("tool_call_id") or "")
            output = _tool_content_to_function_output(content)
            if call_id:
                input_items.append({"type": "function_call_output", "call_id": call_id, "output": output})
            continue

    instructions = str(
        body.get("instructions") or os.getenv("CODEX_DEFAULT_INSTRUCTIONS") or "You are a helpful assistant."
    ).strip()

    payload: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "stream": bool(body.get("stream", False)),
        "store": False,
    }

    # codex_oauth 走的是 chatgpt.com 的 codex responses 端点。
    # 该端点当前不接受 max_output_tokens/max_tokens，故这里不透传 max_tokens。
    # codex_oauth 的 responses 端点当前会拒绝 temperature/top_p，故不透传采样参数。
    if body.get("tool_choice") is not None:
        mapped = _chat_tool_choice_to_responses(body.get("tool_choice"))
        if mapped is not None:
            payload["tool_choice"] = mapped
    if body.get("tools") is not None:
        mapped_tools = _chat_tools_to_responses_tools(body.get("tools"))
        if mapped_tools is not None:
            payload["tools"] = mapped_tools

    # 关键兼容点 1：reasoning.effort 统一由 resolve_codex_upstream_reasoning_effort 解析（含模型 @后缀）。
    raw_reasoning = body.get("reasoning")
    rd = raw_reasoning if isinstance(raw_reasoning, dict) else None
    payload["reasoning"] = {
        "effort": resolve_codex_upstream_reasoning_effort(
            reasoning_effort_top=body.get("reasoning_effort"),
            reasoning_dict=rd,
            model_suffix_effort=model_suffix_effort,
        )
    }

    # 关键兼容点 2：与原生 /v1/responses + codex_oauth 路径一致，见 ensure_codex_responses_include_encrypted_reasoning。
    ensure_codex_responses_include_encrypted_reasoning(payload, include_source=body)

    return payload


def _parse_codex_function_call_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    item_type = str(item.get("type") or "")
    if item_type not in {"function_call", "tool_call"}:
        return None

    name = str(item.get("name") or "")
    if not name:
        return None

    call_id = str(item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex}")
    raw_args = item.get("arguments")
    parsed_input: Dict[str, Any] = {}
    if isinstance(raw_args, dict):
        parsed_input = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            decoded = json.loads(raw_args)
            if isinstance(decoded, dict):
                parsed_input = decoded
        except Exception:
            parsed_input = {}

    return {"id": call_id, "name": name, "input": parsed_input}


def _extract_codex_output_text(resp_json: Dict[str, Any]) -> str:
    out = resp_json.get("output")
    if not isinstance(out, list):
        return ""

    texts: List[str] = []
    for item in out:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            ctype = c.get("type")
            if ctype == "output_text":
                texts.append(str(c.get("text") or ""))
            elif ctype == "text":
                texts.append(str(c.get("text") or ""))
    return "".join(texts)


def _codex_responses_to_chat_completion(resp_json: Dict[str, Any], model: str) -> Dict[str, Any]:
    text = _extract_codex_output_text(resp_json)
    tool_uses = _extract_codex_output_tool_uses(resp_json)
    # 关键兼容点 3（当前行为说明）：
    # chat/completions 的标准返回结构没有 reasoning item 的一等字段。
    # 因此这里仅抽取可见文本与 usage，responses output 里的 encrypted_content
    # 暂不下发到 chat 返回体，避免破坏现有客户端兼容性。
    usage = resp_json.get("usage") if isinstance(resp_json.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = prompt_tokens + completion_tokens

    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_uses:
        message["tool_calls"] = [
            {
                "id": tu["id"],
                "type": "function",
                "function": {
                    "name": tu["name"],
                    "arguments": json.dumps(tu.get("input") or {}, ensure_ascii=False),
                },
            }
            for tu in tool_uses
        ]
    finish_reason = "tool_calls" if tool_uses else "stop"

    return {
        "id": str(resp_json.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


def _extract_codex_output_tool_uses(resp_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = resp_json.get("output")
    if not isinstance(out, list):
        return []

    tool_uses: List[Dict[str, Any]] = []
    for item in out:
        if not isinstance(item, dict):
            continue

        parsed_direct = _parse_codex_function_call_item(item)
        if parsed_direct is not None:
            tool_uses.append(parsed_direct)
            continue

        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            parsed_nested = _parse_codex_function_call_item(part)
            if parsed_nested is not None:
                tool_uses.append(parsed_nested)
    return tool_uses


def _extract_model_and_ban_explore(raw_model: Any, base_ban_explore: bool) -> Tuple[Optional[str], bool]:
    ban_explore = base_ban_explore
    model_from_body: Optional[str] = raw_model if isinstance(raw_model, str) else None
    suffix = "--ban_explore"
    if model_from_body and model_from_body.endswith(suffix):
        ban_explore = True
        model_from_body = model_from_body[: -len(suffix)] or None
    return model_from_body, ban_explore

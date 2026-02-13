import os
import json
import time
import uuid
import asyncio
import re
import glob
from typing import Any, Dict, List, Optional, AsyncIterator, Tuple

import httpx
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

from token_auth import (
    begin_codex_oauth,
    clear_codex_auth,
    complete_codex_oauth_callback,
    get_codex_auth_status,
    get_codex_upstream_headers,
    get_x_auth_token,
)
from upstream_config import (
    PROTOCOL_ANTHROPIC_MESSAGES,
    PROTOCOL_OPENAI_CHAT,
    PROTOCOL_OPENAI_RESPONSES,
    UpstreamCapabilityError,
    UpstreamConfigError,
    build_auth_headers,
    build_upstream_url,
    get_runtime_options,
    get_effective_auth_type,
    load_and_validate_config,
    resolve_profile,
)
load_dotenv(override=True)
import tiktoken
import logging

# 全局默认：是否屏蔽 Task 工具里的 "- Explore:" 行
BAN_EXPLORE = os.getenv("BAN_EXPLORE", "false").lower() == "true"
BAN_STREAM = os.getenv("BAN_STREAM", "false").lower() == "true"
EXPOSE_THINKING = os.getenv("EXPOSE_THINKING", "true").lower() == "true"
UPSTREAM_CONFIG = load_and_validate_config()


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
                        # 参考 node 版实现：JSON 序列化 input
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


app = FastAPI(title="Anthropic+OpenAI Proxy (FastAPI)")




# -----------------------------
# Helpers: Anthropic -> OpenAI mapping
# -----------------------------
def _anthropic_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "".join(parts)
    return str(content)


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
            # 兜底
            text = _extract_text_from_blocks(content)
            out.append({"role": role if role in ("user", "assistant", "system") else "user", "content": text})
            continue

        if role == "assistant":
            # assistant blocks: text / thinking / tool_use
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
                    # Anthropic tool_use: {type:"tool_use", id, name, input}
                    tool_id = b.get("id")
                    name = b.get("name")
                    tool_input = b.get("input", {})
                    if tool_id and name:
                        tool_calls.append({
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(tool_input, ensure_ascii=False),
                            }
                        })

            msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "".join(text_parts)
            }
            if thinking_parts:
                msg["reasoning_content"] = "".join(thinking_parts)
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user":
            # user blocks: text / tool_result (+ 可能混在一个 message 里)
            text_parts: List[str] = []
            tool_results: List[Dict[str, Any]] = []

            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    text_parts.append(b.get("text", ""))
                elif t == "tool_result":
                    # Anthropic tool_result: {type:"tool_result", tool_use_id, content}
                    tool_use_id = b.get("tool_use_id")
                    tool_content = b.get("content")
                    tool_text = _extract_text_from_blocks(tool_content)
                    if tool_use_id:
                        tool_results.append({
                            "tool_call_id": tool_use_id,
                            "content": tool_text
                        })

            # 先把 user 的 text 发出去（如果有的话）
            user_text = "".join(text_parts)
            if user_text.strip():
                out.append({"role": "user", "content": user_text})
            elif not tool_results:
                # 空 user 消息兜底，避免上游报错
                out.append({"role": "user", "content": ""})

            # 再把 tool_result 映射成 OpenAI tool messages（顺序保持在 user 后面）
            for tr in tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "content": tr["content"]
                })
            continue

        # 其他 role 兜底
        out.append({"role": "user", "content": _extract_text_from_blocks(content)})

    return out



def anthropic_tools_to_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """
    Anthropic tools[]: [{name, description, input_schema}]  ->
    OpenAI tools[]:    [{type:"function", function:{name, description, parameters}}]
    """
    if not tools:
        return None

    out = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            }
        })
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
        # Anthropic tools: {name, description, input_schema}
        if t.get("name") == "Task":
            new_desc = _remove_explore_from_desc(t.get("description"))
            if new_desc is not None:
                cleaned.append({**t, "description": new_desc})
            else:
                cleaned.append(t)
            continue

        # OpenAI tools: {type:"function", function:{name, description, parameters}}
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
    """
    Anthropic tool_choice:
      {"type":"auto"|"none"|"any"} or {"type":"tool","name":"xxx", ...}
    ->
    OpenAI tool_choice:
      "auto" | "none" | {"type":"function","function":{"name":"xxx"}}
    """
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

    # Anthropic 的 "any" 表示允许用任意工具/倾向使用工具；OpenAI chat.completions 没有 1:1 同名项
    # 最保守先映射成 "auto"（下一步你如果想“强制必须用工具”，可以再讨论映射到 required 等策略）
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
        # 先兼容常见图片结构；其他类型先降级为文本
        if p_type == "image_url" and isinstance(part.get("image_url"), dict):
            url = str(part["image_url"].get("url") or "")
            if url:
                out.append({"type": "input_image", "image_url": url})
            continue
        fallback = _message_content_to_text(part)
        if fallback:
            out.append({"type": "input_text", "text": fallback})
    return out


def _build_codex_responses_payload_from_chat(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    input_items: List[Dict[str, Any]] = []

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")

        # 贴近 opencode：system 不合并掉，而是以 system/developer 角色进入 input。
        if role in {"system", "developer"}:
            text = _message_content_to_text(content)
            if text:
                input_items.append(
                    {
                        "role": "developer" if role == "developer" else "system",
                        "content": text,
                    }
                )
            continue

        if role == "user":
            parts = _chat_content_to_responses_input_parts(content)
            if parts:
                input_items.append(
                    {
                        "role": "user",
                        "content": parts,
                    }
                )
            continue

        if role == "assistant":
            text = _message_content_to_text(content)
            if text:
                input_items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
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
                        {
                            "type": "function_call",
                            "call_id": tc_id,
                            "name": name,
                            "arguments": args_str,
                        }
                    )
            continue

        if role == "tool":
            call_id = str(m.get("tool_call_id") or "")
            text = _message_content_to_text(content)
            if call_id:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": text,
                    }
                )
            continue

    # 保持 instructions 独立（贴近 opencode：与 input 并行存在）
    instructions = str(
        body.get("instructions")
        or os.getenv("CODEX_DEFAULT_INSTRUCTIONS")
        or "You are a helpful assistant."
    ).strip()

    payload: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "stream": bool(body.get("stream", False)),
        # 对齐 opencode：Codex/Responses 路径显式关闭 store
        "store": False,
    }

    if body.get("max_tokens") is not None:
        payload["max_output_tokens"] = body.get("max_tokens")
    if body.get("temperature") is not None:
        payload["temperature"] = body.get("temperature")
    if body.get("top_p") is not None:
        payload["top_p"] = body.get("top_p")
    if body.get("tool_choice") is not None:
        mapped = _chat_tool_choice_to_responses(body.get("tool_choice"))
        if mapped is not None:
            payload["tool_choice"] = mapped
    if body.get("tools") is not None:
        mapped_tools = _chat_tools_to_responses_tools(body.get("tools"))
        if mapped_tools is not None:
            payload["tools"] = mapped_tools

    return payload


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
    usage = resp_json.get("usage") if isinstance(resp_json.get("usage"), dict) else {}
    prompt_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total_tokens = prompt_tokens + completion_tokens

    return {
        "id": str(resp_json.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }


# -----------------------------
# Upstream call
# -----------------------------
def _extract_model_and_ban_explore(raw_model: Any, base_ban_explore: bool) -> Tuple[Optional[str], bool]:
    ban_explore = base_ban_explore
    model_from_body: Optional[str] = raw_model if isinstance(raw_model, str) else None
    suffix = "--ban_explore"
    if model_from_body and model_from_body.endswith(suffix):
        ban_explore = True
        model_from_body = model_from_body[: -len(suffix)] or None
    return model_from_body, ban_explore


async def _build_headers_by_profile(profile: Dict[str, Any], model: str) -> Dict[str, str]:
    auth_type = get_effective_auth_type(profile)
    if auth_type == "codex_oauth":
        return await get_codex_upstream_headers(profile)
    if auth_type == "internal_hw":
        token = await get_x_auth_token()
        return build_auth_headers(profile, model, x_auth_token=token)
    return build_auth_headers(profile, model)


# -----------------------------
# Rate limit helpers
# -----------------------------
# 所有需要视为「限流且可重试」的上游状态码，统一维护在这里，便于后续扩展（如再加入 503 等）
RATE_LIMIT_STATUS_CODES = {406, 429}


def is_rate_limit_status(status_code: int) -> bool:
    """
    判断上游响应码是否属于「限流/可重试」错误。
    所有调用处统一依赖本函数，而不是直接写死 (406, 429)。
    """
    return status_code in RATE_LIMIT_STATUS_CODES


# -----------------------------
# Endpoints
# -----------------------------
@app.get("/health")
async def health():
    return {"ok": True}


def _codex_oauth_success_html() -> str:
    return """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Codex 登录成功</title>
    <style>
      body { font-family: system-ui, -apple-system, sans-serif; background: #131010; color: #f1ecec; margin: 0; display: flex; align-items: center; justify-content: center; height: 100vh; }
      .card { text-align: center; padding: 24px; }
      .muted { color: #b7b1b1; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>登录成功</h1>
      <p class="muted">你可以关闭此页面并回到 LLM_PROXY。</p>
    </div>
    <script>setTimeout(() => window.close(), 1800)</script>
  </body>
</html>"""


def _codex_oauth_error_html(msg: str) -> str:
    safe = (msg or "unknown error").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Codex 登录失败</title>
    <style>
      body {{ font-family: system-ui, -apple-system, sans-serif; background: #131010; color: #f1ecec; margin: 0; display: flex; align-items: center; justify-content: center; height: 100vh; }}
      .card {{ text-align: center; padding: 24px; }}
      .error {{ color: #ff917b; background: #3c140d; border-radius: 8px; padding: 12px; margin-top: 12px; font-family: monospace; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>登录失败</h1>
      <div class="error">{safe}</div>
    </div>
  </body>
</html>"""


@app.get("/auth/codex/status")
async def codex_auth_status():
    return await get_codex_auth_status()


@app.get("/auth/codex/login")
async def codex_auth_login():
    try:
        flow = await begin_codex_oauth()
        return {
            "ok": True,
            **flow,
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/auth/codex/callback")
async def codex_auth_callback(req: Request):
    code = req.query_params.get("code")
    state = req.query_params.get("state") or ""
    error = req.query_params.get("error")
    error_description = req.query_params.get("error_description")
    try:
        await complete_codex_oauth_callback(
            state=state,
            code=code,
            error=error,
            error_description=error_description,
        )
        return Response(content=_codex_oauth_success_html(), media_type="text/html")
    except ValueError as e:
        return Response(content=_codex_oauth_error_html(str(e)), media_type="text/html", status_code=400)
    except Exception as e:
        return Response(content=_codex_oauth_error_html(str(e)), media_type="text/html", status_code=500)


@app.post("/auth/codex/logout")
async def codex_auth_logout():
    await clear_codex_auth()
    return {"ok": True}


# ---------- Anthropic Messages ----------
def _dump_json(path: str, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

def _resp_to_obj(r):  # httpx.Response -> dict
    base = {"status_code": r.status_code, "headers": dict(r.headers)}
    try:
        base["json"] = r.json()
    except Exception:
        base["text"] = r.text
    return base


def _extract_first_user_text(body: Dict[str, Any]) -> str:
    """
    提取首条 user 消息的 text 内容，用于 warmup 识别。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return ""
    first = msgs[0] or {}
    content = first.get("content")
    return _extract_text_from_blocks(content).strip().lower()


def _system_texts(body: Dict[str, Any]) -> List[str]:
    """
    拉平 system 字段的所有 text 段，便于关键词匹配。
    """
    systems = body.get("system")
    if systems is None:
        return []
    if isinstance(systems, list):
        texts = []
        for s in systems:
            if isinstance(s, dict):
                texts.append(_extract_text_from_blocks(s.get("text")))
            else:
                texts.append(_extract_text_from_blocks(s))
        return texts
    return [_extract_text_from_blocks(systems)]


def _should_skip_session_logging(body: Dict[str, Any]) -> bool:
    """
    按规则过滤不需要写入 session 目录的请求：
    - warmup：首个 user content 为 'warmup'（不区分大小写）
    - topic：system 含 “Analyze if this message indicates a new conversation topic”
    - summary：system 含 “Summarize this coding conversation”
    """
    first_text = _extract_first_user_text(body)
    # _extract_first_user_text 已经 lower，直接匹配小写
    if first_text == "warmup":
        return True

    sys_texts = " ".join(t.lower() for t in _system_texts(body))
    if "analyze if this message indicates a new conversation topic" in sys_texts:
        return True
    if "summarize this coding conversation" in sys_texts:
        return True
    return False


def _discard_session_req(session_req_path: Optional[str]) -> None:
    if session_req_path and os.path.exists(session_req_path):
        try:
            os.remove(session_req_path)
        except:
            pass


def _usage_dict_has_tokens(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(k in usage for k in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"))


def _extract_usage_from_obj(obj: Any) -> Optional[Dict[str, Any]]:
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            return obj.get("usage")
        if isinstance(obj.get("json"), dict) and isinstance(obj["json"].get("usage"), dict):
            return obj["json"].get("usage")
        if isinstance(obj.get("text"), str):
            try:
                parsed = json.loads(obj.get("text"))
                if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
                    return parsed.get("usage")
            except Exception:
                pass
    return None


def _parse_anthropic_sse_chunks_to_events(chunks: List[Any]) -> List[Dict[str, Any]]:
    raw_text = []
    for chunk in chunks:
        if isinstance(chunk, bytes):
            raw_text.append(chunk.decode("utf-8", errors="replace"))
        elif isinstance(chunk, str):
            raw_text.append(chunk)
    text = "".join(raw_text)
    lines = text.splitlines()
    events: List[Dict[str, Any]] = []
    current_event = None
    for line in lines:
        line = line.rstrip("\r")
        if not line:
            current_event = None
            continue
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                data = {"_raw": data_str}
            event = current_event
            if event is None and isinstance(data, dict):
                event = data.get("type")
            events.append({"event": event, "data": data})
    return events


def _build_anthropic_non_stream_from_events(
    events: List[Dict[str, Any]],
    fallback_model: str,
) -> Optional[Dict[str, Any]]:
    if not events:
        return None
    resp: Dict[str, Any] = {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": fallback_model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
    }
    blocks: Dict[int, Dict[str, Any]] = {}
    input_buffers: Dict[int, str] = {}

    for ev in events:
        event = ev.get("event")
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        if not event:
            event = data.get("type")

        if event == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                resp["id"] = msg.get("id") or resp["id"]
                resp["role"] = msg.get("role") or resp["role"]
                resp["model"] = msg.get("model") or resp["model"]
                resp["stop_reason"] = msg.get("stop_reason")
                resp["stop_sequence"] = msg.get("stop_sequence")
                if isinstance(msg.get("usage"), dict):
                    resp["usage"] = dict(msg.get("usage"))
            continue

        if event == "content_block_start":
            idx = data.get("index")
            cb = data.get("content_block") or {}
            if idx is None or not isinstance(cb, dict):
                continue
            block = dict(cb)
            if block.get("type") == "text":
                block.setdefault("text", "")
            elif block.get("type") == "thinking":
                block.setdefault("thinking", "")
            elif block.get("type") == "tool_use":
                block.setdefault("input", {})
                input_buffers[idx] = ""
            blocks[idx] = block
            continue

        if event == "content_block_delta":
            idx = data.get("index")
            delta = data.get("delta") or {}
            if idx is None or not isinstance(delta, dict):
                continue
            block = blocks.get(idx)
            if not block:
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["text"] = (block.get("text") or "") + (delta.get("text") or "")
            elif delta_type == "thinking_delta":
                block["thinking"] = (block.get("thinking") or "") + (delta.get("thinking") or "")
            elif delta_type == "input_json_delta":
                input_buffers[idx] = input_buffers.get(idx, "") + (delta.get("partial_json") or "")
            continue

        if event == "message_delta":
            delta = data.get("delta") or {}
            if isinstance(delta, dict):
                if delta.get("stop_reason") is not None:
                    resp["stop_reason"] = delta.get("stop_reason")
            usage = data.get("usage")
            if isinstance(usage, dict):
                resp["usage"] = dict(usage)
            continue

    for idx, buf in input_buffers.items():
        if not buf:
            continue
        block = blocks.get(idx)
        if not block or block.get("type") != "tool_use":
            continue
        try:
            block["input"] = json.loads(buf)
        except Exception:
            block["input"] = {"_raw_input": buf}

    if blocks:
        resp["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return resp


async def _forward_anthropic_native_messages(
    body: Dict[str, Any],
    stream: bool,
    profile: Dict[str, Any],
    model: str,
    req_path: str,
    up_res_path: str,
    down_res_path: str,
    session_req_path: Optional[str],
    session_down_res_path: Optional[str],
    session_non_stream_path: Optional[str],
) -> Response:
    upstream_url = build_upstream_url(profile, PROTOCOL_ANTHROPIC_MESSAGES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    payload = dict(body)
    payload["model"] = model

    if not stream:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            r = None
            last_retry_response = None
            headers = await _build_headers_by_profile(profile, model)

            for attempt in range(max_retries):
                r = await client.post(upstream_url, headers=headers, json=payload)
                if not is_rate_limit_status(r.status_code):
                    break
                last_retry_response = r
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    headers = await _build_headers_by_profile(profile, model)

            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        _dump_json(up_res_path, _resp_to_obj(r))
        down_obj = {
            "type": "anthropic_passthrough_response",
            "status_code": r.status_code,
            "headers": dict(r.headers),
        }
        try:
            down_obj["json"] = r.json()
        except Exception:
            down_obj["text"] = r.text
        _dump_json(down_res_path, down_obj)
        if session_down_res_path:
            usage = _extract_usage_from_obj(down_obj)
            if not _usage_dict_has_tokens(usage):
                _discard_session_req(session_req_path)
            else:
                _dump_json(session_down_res_path, down_obj)

        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        down_chunks: List[Any] = []
        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                last_retry_err_text = None
                last_retry_status = None
                connection_established = False
                retry_headers = await _build_headers_by_profile(profile, model)

                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=payload) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        connection_established = True
                        async for chunk in r.aiter_raw():
                            down_chunks.append(chunk.decode("utf-8", errors="replace"))
                            yield chunk
                        return

                    if not connection_established and attempt < max_retries - 1:
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)

                if (not connection_established) and (last_retry_status is not None):
                    if last_retry_err_text:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    return
        finally:
            _dump_json(up_res_path, {"type": "anthropic_native_sse_capture", "chunks": up_chunks})
            _dump_json(down_res_path, {"type": "anthropic_native_sse_capture", "chunks": down_chunks})
            if session_down_res_path:
                events = _parse_anthropic_sse_chunks_to_events(down_chunks)
                non_stream_resp = _build_anthropic_non_stream_from_events(events, model)
                usage = _extract_usage_from_obj(non_stream_resp) if non_stream_resp else None
                if not _usage_dict_has_tokens(usage):
                    _discard_session_req(session_req_path)
                else:
                    _dump_json(session_down_res_path, {"type": "anthropic_native_sse_capture", "chunks": down_chunks})
                    if session_non_stream_path and non_stream_resp:
                        _dump_json(session_non_stream_path, non_stream_resp)

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")


@app.post("/v1/responses")
async def openai_responses(req: Request):
    """
    OpenAI Responses endpoint pass-through:
      - non-stream: upstream JSON pass-through
      - stream: upstream SSE pass-through
    """
    body = await req.json()
    body_model = body.get("model")
    model_from_body, _ = _extract_model_and_ban_explore(body_model, BAN_EXPLORE)
    if model_from_body is not None:
        body["model"] = model_from_body
    stream = bool(body.get("stream", False))

    try:
        resolved = resolve_profile(UPSTREAM_CONFIG, body, PROTOCOL_OPENAI_RESPONSES)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model

    os.makedirs("logs_openai", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
    req_path = os.path.join("logs_openai", f"{ts}-responses-req.json")
    res_path = os.path.join("logs_openai", f"{ts}-responses-res.json")

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_RESPONSES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await _build_headers_by_profile(profile, model)
    body["model"] = model
    if get_effective_auth_type(profile) == "codex_oauth":
        # 对齐 opencode：Codex 要求 store=false，用户传 true 时也要覆盖
        body["store"] = False

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    _dump_json(req_path, log_body)

    if not stream:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            r = None
            last_retry_response = None
            for attempt in range(max_retries):
                r = await client.post(upstream_url, headers=upstream_headers, json=body)
                if not is_rate_limit_status(r.status_code):
                    break
                last_retry_response = r
                logging.warning(f"{attempt} retryable response (responses non-stream): {r.status_code} {r.text}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await _build_headers_by_profile(profile, model)
            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        _dump_json(res_path, _resp_to_obj(r))
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def sse_passthrough() -> AsyncIterator[bytes]:
        chunks: List[Any] = []
        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                retry_headers = upstream_headers
                last_retry_err_text = None
                last_retry_status = None
                connected = False
                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                        chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        if is_rate_limit_status(r.status_code):
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            chunks.append({"type": "error_body", "body": last_retry_err_text})
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        connected = True
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            chunks.append({"type": "error_body", "body": err_text})
                            yield err
                            return

                        async for raw in r.aiter_raw():
                            chunks.append(raw.decode("utf-8", errors="replace"))
                            yield raw
                        break

                if not connected and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    if last_retry_err_text is not None:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    return
        finally:
            _dump_json(res_path, {"type": "responses_passthrough_sse_capture", "chunks": chunks})

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")


@app.post("/v1/messages")
async def v1_messages(req: Request):
    body = await req.json()
    # 临时禁用流式请求：直接返回错误，不记录任何日志
    body_stream = bool(body.get("stream", False))
    header_stream = req.headers.get("x-stainless-helper-method", "").lower() == "stream"
    stream = body_stream or header_stream
    if stream and BAN_STREAM:
        return JSONResponse(
            {
                "error": {
                    "message": "暂不支持流式请求，请使用非流式模式重试",
                    "type": "stream_disabled",
                }
            },
            status_code=400,
        )
    os.makedirs("logs_anthropic", exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名

    session_id = None
    session_metadata = body.get("metadata")
    if isinstance(session_metadata, dict):
        user_id = session_metadata.get("user_id") or ""
        m = re.search(r"session_([A-Za-z0-9-]+)", str(user_id))
        if m:
            session_id = m.group(1)
    skip_session_logging = False
    if session_id:
        skip_session_logging = _should_skip_session_logging(body)

    req_path = os.path.join("logs_anthropic", f"{ts}-req.json")
    up_res_path = os.path.join("logs_anthropic", f"{ts}-upstream-res.json")
    down_res_path = os.path.join("logs_anthropic", f"{ts}-downstream-res.json")
    headers_path = os.path.join("logs_anthropic", f"{ts}-headers.json")

    session_req_path = None
    session_down_res_path = None
    session_non_stream_path = None
    if session_id and not skip_session_logging:
        # 若该 session_id 已有目录则复用，否则按当前时间戳新建
        os.makedirs("logs_session", exist_ok=True)
        existing_dirs = sorted(glob.glob(os.path.join("logs_session", f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join("logs_session", f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        session_req_path = os.path.join(session_dir, f"{ts}-req.json")
        session_down_res_path = os.path.join(session_dir, f"{ts}-downstream-res.json")
        session_non_stream_path = os.path.join(session_dir, f"{ts}-non-stream-res.json")

    # ---- 解析 model & ban_explore ----
    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, BAN_EXPLORE)
    if model_from_body is not None:
        body["model"] = model_from_body

    max_tokens = int(body.get("max_tokens", 1024))
    messages = body.get("messages", [])
    system = body.get("system", None)
    temperature = body.get("temperature", None)
    top_p = body.get("top_p", None)
    stop_sequences = body.get("stop_sequences", None)
    tools = body.get("tools")
    tool_choice = body.get("tool_choice")
    thinking = body.get("thinking")

    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(tools, ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    try:
        resolved = resolve_profile(UPSTREAM_CONFIG, body, PROTOCOL_ANTHROPIC_MESSAGES)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")

    _dump_json(headers_path, dict(req.headers))
    _dump_json(req_path, log_body)
    if session_req_path:
        _dump_json(session_req_path, log_body)

    if profile.get("provider") == "anthropic":
        return await _forward_anthropic_native_messages(
            body=body,
            stream=stream,
            profile=profile,
            model=model,
            req_path=req_path,
            up_res_path=up_res_path,
            down_res_path=down_res_path,
            session_req_path=session_req_path,
            session_down_res_path=session_down_res_path,
            session_non_stream_path=session_non_stream_path,
        )

    oai_messages = anthropic_messages_to_openai(messages, system)

    upstream_payload: Dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if thinking is not None:
        upstream_payload["thinking"] = thinking

    oai_tools = anthropic_tools_to_openai_tools(tools)
    if oai_tools:
        upstream_payload["tools"] = oai_tools

    oai_tool_choice = anthropic_tool_choice_to_openai(tool_choice)
    if oai_tool_choice is not None:
        upstream_payload["tool_choice"] = oai_tool_choice

    if isinstance(tool_choice, dict) and "disable_parallel_tool_use" in tool_choice:
        upstream_payload["parallel_tool_calls"] = (not bool(tool_choice["disable_parallel_tool_use"]))

    if temperature is not None:
        upstream_payload["temperature"] = temperature
    if top_p is not None:
        upstream_payload["top_p"] = top_p
    if stop_sequences is not None:
        upstream_payload["stop"] = stop_sequences
    if stream:
        upstream_payload["stream_options"] = {"include_usage": True}


    upstream_url = build_upstream_url(profile, PROTOCOL_ANTHROPIC_MESSAGES)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await _build_headers_by_profile(profile, model)

    # ---- non-stream ----
    if not stream:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            # 限流状态码重试逻辑：重试次数来自 profile 配置
            r = None
            last_retry_response = None
            
            for attempt in range(max_retries):
                r = await client.post(upstream_url, headers=upstream_headers, json=upstream_payload)
                
                if not is_rate_limit_status(r.status_code):
                    # 不是限流错误，直接使用这个响应
                    break
                
                # 是限流错误，保存响应用于最后返回
                last_retry_response = r
                logging.warning(f"{attempt} retryable response: {r.status_code} {r.text}")
                # 如果不是最后一次重试，等待后继续
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await _build_headers_by_profile(profile, model)
            
            # 如果所有重试都是限流错误，使用最后一次的响应
            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        # 1) 存 upstream 原始返回（成功/失败都存）
        up_obj = _resp_to_obj(r)
        _dump_json(up_res_path, up_obj)

        # upstream 错误：下游是透传 Response，这个也存一份“下游实际返回长啥样”
        if r.status_code >= 400:
            down_obj = {
                "type": "passthrough_error",
                "status_code": r.status_code,
                "media_type": r.headers.get("content-type", "application/json"),
                "body": (r.text if r.text is not None else ""),
            }
            _dump_json(down_res_path, down_obj)
            if session_down_res_path:
                # 检查是否包含 usage 字段 (有些错误返回也会带 usage，如果有则存，没有则不存)
                has_usage = False
                try:
                    body_json = json.loads(down_obj["body"])
                    if isinstance(body_json, dict) and "usage" in body_json:
                        has_usage = True
                except:
                    pass

                if not has_usage:
                    # 如果不包含 usage，则不写入 res 到 session，且把已写入的 req 也删掉
                    if session_req_path and os.path.exists(session_req_path):
                        try:
                            os.remove(session_req_path)
                        except:
                            pass
                else:
                    _dump_json(session_down_res_path, down_obj)
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "application/json"),
            )

        # 2) 正常：你原来的整理逻辑
        data = r.json()
        usage = data.get("usage")
        if session_down_res_path and usage is None:
            # 如果没有 usage 字段，则认为是不正常返回，不存 session，且把已写入的 req 也删掉
            if session_req_path and os.path.exists(session_req_path):
                try:
                    os.remove(session_req_path)
                except:
                    pass
            # 标记为 None，后续不再写入 session_down_res_path
            session_down_res_path = None

        usage = usage or {}
        cache_creation = usage.get("cache_creation") or {}
        if not isinstance(cache_creation, dict):
            cache_creation = {}
        server_tool_use = usage.get("server_tool_use")
        if not isinstance(server_tool_use, dict):
            server_tool_use = {"web_search_requests": 0}
        service_tier = usage.get("service_tier") or "standard"

        choice0 = (data.get("choices") or [None])[0] or {}
        finish_reason = choice0.get("finish_reason")
        msg = choice0.get("message") or {}

        assistant_text = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        content_blocks = []

        # 1) thinking（如果开启且上游有 reasoning_content）
        if EXPOSE_THINKING:
            rc = msg.get("reasoning_content")
            if rc:
                content_blocks.append({"type": "thinking", "thinking": rc})

        # 2) final text
        if assistant_text:
            content_blocks.append({"type": "text", "text": assistant_text})

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or "unknown_tool"
            tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex}"
            args_str = fn.get("arguments") or "{}"
            try:
                tool_input = json.loads(args_str) if isinstance(args_str, str) else (args_str or {})
            except Exception:
                tool_input = {"_raw_arguments": args_str}

            content_blocks.append({
                "type": "tool_use",
                "id": tool_id,
                "name": name,
                "input": tool_input,
            })

        # 重新映射 stop_reason
        anthropic_stop_reason = oai_finish_reason_to_stop_reason(finish_reason)
        # 如果有 tool_calls，通常强制为 tool_use
        if tool_calls and anthropic_stop_reason != "tool_use":
            anthropic_stop_reason = "tool_use"

        resp = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content_blocks,
            "stop_reason": anthropic_stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation": {
                    "ephemeral_1h_input_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0),
                    "ephemeral_5m_input_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0),
                },
                "server_tool_use": server_tool_use,
                "service_tier": service_tier,
            },
        }
        _dump_json(down_res_path, resp)
        if session_down_res_path:
            _dump_json(session_down_res_path, resp)
        return JSONResponse(resp)

    # ---- stream SSE ----
    async def sse() -> AsyncIterator[bytes]:
        up_chunks = []    # upstream chunks
        down_events = []  # downstream events

        def emit(event: str, data: Dict[str, Any]) -> bytes:
            # Anthropic SSE: data 里需要带 type 字段（和 event 同名）
            if isinstance(data, dict) and "type" not in data:
                data = {"type": event, **data}
            down_events.append({"event": event, "data": data})
            return _sse_event(event, data)

        msg_id = f"msg_{uuid.uuid4().hex}"

        # Usage tracking
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        usage_received = False
        final_finish_reason: Optional[str] = None

        def _update_usage_from_obj(obj: Any) -> None:
            nonlocal prompt_tokens, completion_tokens, total_tokens, usage_received
            if not isinstance(obj, dict):
                return
            u = obj.get("usage")
            if isinstance(u, dict):
                usage_received = True
                if u.get("prompt_tokens") is not None:
                    prompt_tokens = int(u.get("prompt_tokens") or prompt_tokens)
                if u.get("completion_tokens") is not None:
                    completion_tokens = int(u.get("completion_tokens") or completion_tokens)
                if u.get("total_tokens") is not None:
                    total_tokens = int(u.get("total_tokens") or total_tokens)
            # Some providers put usage in choices
            choices = obj.get("choices")
            if isinstance(choices, list) and choices:
                c0 = choices[0] or {}
                if isinstance(c0, dict):
                    u2 = c0.get("usage")
                    if isinstance(u2, dict):
                        usage_received = True
                        if u2.get("prompt_tokens") is not None:
                            prompt_tokens = int(u2.get("prompt_tokens") or prompt_tokens)
                        if u2.get("completion_tokens") is not None:
                            completion_tokens = int(u2.get("completion_tokens") or completion_tokens)

        # State machine for content blocks
        # 目标：与 Anthropic 正常语义一致（thinking=0, text=1，避免跳号）
        current_block_index = 0
        current_block_type = None  # "thinking" | "text" | "tool_use"
        thinking_started = False
        text_started = False
        tool_map: Dict[int, Dict[str, Any]] = {}  # openai_tool_index -> {block_index, id, name}
        has_started = False

        try:
            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                # 限流状态码重试逻辑：重试次数来自 profile 配置
                last_retry_err_text = None
                last_retry_status = None
                retry_headers = upstream_headers
                connection_established = False
                
                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=upstream_payload) as r:
                        up_chunks.append({"type": "response_meta", "status_code": r.status_code, "headers": dict(r.headers)})
                        
                        if is_rate_limit_status(r.status_code):
                            # 是限流错误，保存错误信息并关闭连接
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", "ignore")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "text": last_retry_err_text})
                            # 关闭连接，准备重试（退出async with块）
                            break
                        
                        # 不是限流错误，继续在这个连接上处理
                        connection_established = True
                        
                        # 处理其他错误（非406）
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", "ignore")
                            up_chunks.append({"type": "error_body", "text": err_text})
                            yield emit("error", {"upstream_status": r.status_code, "upstream_body": err_text})
                            yield emit("message_stop", {})
                            return

                        # 正常情况，读取流数据
                        async for line in r.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                up_chunks.append({"type": "done"})
                                break

                            try:
                                chunk = json.loads(data_str)
                                up_chunks.append({"type": "chunk", "json": chunk})
                            except Exception:
                                up_chunks.append({"type": "chunk", "raw": data_str})
                                continue

                            _update_usage_from_obj(chunk)
                            
                            # Send message_start on first chunk
                            if not has_started:
                                has_started = True
                                yield emit("message_start", {
                                    "message": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "model": chunk.get("model", model),
                                        "content": [],
                                        "stop_reason": None,
                                        "stop_sequence": None,
                                        "usage": {"input_tokens": prompt_tokens, "output_tokens": 0},
                                    }
                                })

                            choice0 = (chunk.get("choices") or [None])[0] or {}
                            delta = choice0.get("delta") or {}
                            fr = choice0.get("finish_reason")
                            if fr:
                                final_finish_reason = fr

                            # 0. Reasoning Content
                            rc = delta.get("reasoning_content")
                            if rc and EXPOSE_THINKING:
                                # 如果已经进入文本块，后续 reasoning_content 忽略以保持索引稳定
                                if text_started:
                                    continue
                                if current_block_type is not None and current_block_type != "thinking":
                                    yield emit("content_block_stop", {"index": current_block_index})
                                    current_block_type = None

                                if current_block_type is None:
                                    if not thinking_started:
                                        current_block_index = 0
                                        thinking_started = True
                                    yield emit("content_block_start", {
                                        "index": current_block_index,
                                        "content_block": {"type": "thinking", "thinking": ""}
                                    })
                                    current_block_type = "thinking"

                                yield emit("content_block_delta", {
                                    "index": current_block_index,
                                    "delta": {"type": "thinking_delta", "thinking": rc}
                                })

                            # 1. Text Content
                            txt = delta.get("content")
                            if txt is not None:
                                # If we were in tool mode or thinking mode, or this is first block
                                if current_block_type is not None and current_block_type != "text":
                                    yield emit("content_block_stop", {"index": current_block_index})
                                    current_block_type = None
                                
                                if current_block_type is None:
                                    if not text_started:
                                        current_block_index = 1 if thinking_started else 0
                                        text_started = True
                                    yield emit("content_block_start", {
                                        "index": current_block_index,
                                        "content_block": {"type": "text", "text": ""}
                                    })
                                    current_block_type = "text"
                                
                                yield emit("content_block_delta", {
                                    "index": current_block_index,
                                    "delta": {"type": "text_delta", "text": txt}
                                })

                            # 2. Tool Calls
                            tcs = delta.get("tool_calls")
                            if tcs:
                                for tc in tcs:
                                    idx = tc.get("index")
                                    if idx is None:
                                        continue
                                    
                                    # Check if new tool or existing
                                    if idx not in tool_map:
                                        # New tool call -> start new block
                                        if current_block_type is not None:
                                            yield emit("content_block_stop", {"index": current_block_index})
                                            current_block_type = None
                                        
                                        # tool_use 的起始索引应在 thinking/text 之后
                                        if not tool_map:
                                            base_index = 0
                                            if thinking_started:
                                                base_index += 1
                                            if text_started:
                                                base_index += 1
                                            current_block_index = base_index
                                        else:
                                            current_block_index += 1
                                        
                                        t_id = tc.get("id") or f"toolu_{uuid.uuid4().hex}"
                                        fn = tc.get("function") or {}
                                        t_name = fn.get("name") or "unknown"
                                        
                                        tool_map[idx] = {
                                            "block_index": current_block_index,
                                            "id": t_id,
                                            "name": t_name
                                        }
                                        current_block_type = "tool_use"
                                        
                                        yield emit("content_block_start", {
                                            "index": current_block_index,
                                            "content_block": {
                                                "type": "tool_use",
                                                "id": t_id,
                                                "name": t_name,
                                                "input": {}
                                            }
                                        })
                                    
                                    # Tool arguments
                                    fn = tc.get("function") or {}
                                    args = fn.get("arguments")
                                    if args:
                                        b_idx = tool_map[idx]["block_index"]
                                        yield emit("content_block_delta", {
                                            "index": b_idx,
                                            "delta": {"type": "input_json_delta", "partial_json": args}
                                        })

                        # Cleanup：只有在成功建立连接时才发送 message_stop 并 return
                        if connection_established:
                            if current_block_type is not None:
                                yield emit("content_block_stop", {"index": current_block_index})
                            
                            stop_reason = oai_finish_reason_to_stop_reason(final_finish_reason) or "end_turn"
                            # If we had tool calls, force tool_use as stop reason if not already
                            if tool_map and stop_reason != "tool_use":
                                # Only if the finish reason wasn't explicitly something else like error/length
                                if final_finish_reason == "tool_calls":
                                    stop_reason = "tool_use"

                            yield emit("message_delta", {
                                "delta": {"stop_reason": stop_reason},
                                "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
                            })
                            yield emit("message_stop", {})
                            return
                    
                    # 如果不是最后一次重试且连接失败（限流错误），等待后继续
                    if not connection_established and attempt < max_retries - 1:
                        # 指数退避：0.1s, 0.2s, 0.4s, 0.8s
                        await asyncio.sleep(0.1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)
                
                # 如果所有重试都是限流错误，返回错误
                if not connection_established and last_retry_status is not None and is_rate_limit_status(last_retry_status):
                    yield emit("error", {"upstream_status": last_retry_status, "upstream_body": last_retry_err_text})
                    yield emit("message_stop", {})
                    return

        finally:
            _dump_json(up_res_path, {"type": "openai_sse_capture", "chunks": up_chunks})
            _dump_json(down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
            if session_down_res_path:
                if not usage_received:
                    # 如果没有收到 usage 字段，则认为是不正常返回，不存 session，且把已写入的 req 也删掉
                    _discard_session_req(session_req_path)
                else:
                    _dump_json(session_down_res_path, {"type": "anthropic_sse_capture", "events": down_events})
                    non_stream_resp = _build_anthropic_non_stream_from_events(down_events, model)
                    usage = _extract_usage_from_obj(non_stream_resp) if non_stream_resp else None
                    if session_non_stream_path and non_stream_resp and _usage_dict_has_tokens(usage):
                        _dump_json(session_non_stream_path, non_stream_resp)

    return StreamingResponse(sse(), media_type="text/event-stream")



def _sse_event(event: str, data: Any) -> bytes:
    payload = json.dumps(data, ensure_ascii=False)
    return (f"event: {event}\n" f"data: {payload}\n\n").encode("utf-8")


# ---------- Session Stats ----------
def _collect_usage_tokens(obj: Any) -> (int, int):
    """
    从响应对象中提取 input/output tokens。
    - 直接读取 obj.usage
    - _resp_to_obj 结构下，尝试从 obj.json.usage 读取
    - SSE capture 尝试从 events[].data.usage 里累加
    - openai_passthrough_sse_capture 尝试从 chunks 中读取最后一个 usage
    """
    def _usage_pair(usage: Any) -> (int, int):
        if not isinstance(usage, dict):
            return 0, 0
        try:
            in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            return in_tok, out_tok
        except Exception:
            return 0, 0

    total_in, total_out = 0, 0
    direct_usage = None
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            direct_usage = obj.get("usage")
        elif isinstance(obj.get("json"), dict):
            direct_usage = obj.get("json", {}).get("usage")

    if direct_usage is not None:
        return _usage_pair(direct_usage)

    # SSE capture 结构：{"type": "...", "events": [{"data": {...}} ...]}
    usage_found = False
    if isinstance(obj, dict):
        events = obj.get("events")
        if isinstance(events, list):
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                data = ev.get("data")
                if not isinstance(data, dict):
                    continue
                in_tok, out_tok = _usage_pair(data.get("usage"))
                if in_tok or out_tok:
                    usage_found = True
                total_in += in_tok
                total_out += out_tok

        # OpenAI 直通 SSE capture：{"type": "openai_passthrough_sse_capture", "chunks": [...]}
        if not usage_found:
            last_in = 0
            last_out = 0
            chunks = obj.get("chunks")
            if isinstance(chunks, list):
                for chunk in chunks:
                    if isinstance(chunk, dict):
                        in_tok, out_tok = _usage_pair(chunk.get("usage"))
                        if in_tok or out_tok:
                            last_in, last_out = in_tok, out_tok
                        if isinstance(chunk.get("json"), dict):
                            in_tok, out_tok = _usage_pair(chunk.get("json", {}).get("usage"))
                            if in_tok or out_tok:
                                last_in, last_out = in_tok, out_tok
                        continue
                    if not isinstance(chunk, str):
                        continue
                    line = chunk.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        data = json.loads(data_str)
                    except Exception:
                        continue
                    in_tok, out_tok = _usage_pair(data.get("usage"))
                    if in_tok or out_tok:
                        last_in, last_out = in_tok, out_tok
                    choices = data.get("choices")
                    if isinstance(choices, list) and choices:
                        c0 = choices[0] or {}
                        if isinstance(c0, dict):
                            in_tok, out_tok = _usage_pair(c0.get("usage"))
                            if in_tok or out_tok:
                                last_in, last_out = in_tok, out_tok
            total_in += last_in
            total_out += last_out

    return total_in, total_out


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

    session_dirs = sorted(glob.glob(os.path.join("logs_session", f"*_{session_id}")))
    stats = _scan_session_dirs(session_dirs) if session_dirs else None
    if not stats or stats.get("num_turns", 0) == 0:
        # logs_session 无数据时再回退到 logs_codeagent
        session_dirs = sorted(glob.glob(os.path.join("logs_codeagent", f"*_{session_id}")))
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
    """
    OpenAI-compatible endpoint:
      - non-stream: upstream JSON pass-through
      - stream: upstream OpenAI SSE pass-through
    """
    body = await req.json()
    stream = bool(body.get("stream", False))
    body_model = body.get("model")
    model_from_body, ban_explore = _extract_model_and_ban_explore(body_model, BAN_EXPLORE)
    if model_from_body is not None:
        body["model"] = model_from_body

    try:
        resolved = resolve_profile(UPSTREAM_CONFIG, body, PROTOCOL_OPENAI_CHAT)
    except UpstreamCapabilityError as e:
        return JSONResponse({"error": {"message": str(e), "type": "unsupported_for_upstream"}}, status_code=404)
    except UpstreamConfigError as e:
        return JSONResponse({"error": {"message": str(e), "type": "upstream_config_error"}}, status_code=400)

    profile_name = resolved.profile_name
    profile = resolved.profile
    model = resolved.model
    auth_type = get_effective_auth_type(profile)

    ## 适配codeagent获取session id
    session_id = req.headers.get("X-Session-Id")

    # 保存请求/响应日志（OpenAI 直通）
    if session_id:
        os.makedirs("logs_codeagent", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名
        # 若该 session_id 已有目录则复用，否则按当前时间戳新建
        existing_dirs = sorted(glob.glob(os.path.join("logs_codeagent", f"*_{session_id}")))
        session_dir = existing_dirs[0] if existing_dirs else os.path.join("logs_codeagent", f"{ts}_{session_id}")
        os.makedirs(session_dir, exist_ok=True)
        req_path = os.path.join(session_dir, f"{ts}-req.json")
        res_path = os.path.join(session_dir, f"{ts}--res.json")
    else:
        os.makedirs("logs_openai", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]  # 带毫秒，避免并发重名
        req_path = os.path.join("logs_openai", f"{ts}-req.json")
        res_path = os.path.join("logs_openai", f"{ts}--res.json")

    upstream_url = build_upstream_url(profile, PROTOCOL_OPENAI_CHAT)
    verify, timeout_seconds, max_retries, trust_env = get_runtime_options(profile)
    upstream_headers = await _build_headers_by_profile(profile, model)

    # 默认 model：优先用户请求体
    body["model"] = model

    # 根据当前请求是否开启 ban_explore 来处理 Task 工具描述
    tools = _strip_task_explore_line(body.get("tools"), ban_explore=ban_explore)
    if tools is not None:
        body["tools"] = tools
    elif "tools" in body:
        body.pop("tools", None)

    upstream_request_body = body
    if auth_type == "codex_oauth":
        upstream_request_body = _build_codex_responses_payload_from_chat(body, model)

    log_body = dict(body)
    log_body["_upstream_profile"] = profile_name
    log_body["_upstream_provider"] = profile.get("provider")
    if auth_type == "codex_oauth":
        log_body["_upstream_payload_kind"] = "codex_responses"
    _dump_json(req_path, log_body)

    # ---- non-stream ----
    if not stream:
        async with httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=trust_env,
        ) as client:
            # 限流状态码重试逻辑：重试次数来自 profile 配置
            r = None
            last_retry_response = None

            for attempt in range(max_retries):
                if auth_type == "codex_oauth":
                    r = await client.post(upstream_url, headers=upstream_headers, json=upstream_request_body)
                else:
                    r = await client.post(upstream_url, headers=upstream_headers, json=body)

                if not is_rate_limit_status(r.status_code):
                    # 不是限流错误，直接使用这个响应
                    break

                # 是限流错误，保存响应用于最后返回
                last_retry_response = r
                logging.warning(f"{attempt} retryable response (chat/completions non-stream): {r.status_code} {r.text}")
                # 如果不是最后一次重试，等待后继续
                if attempt < max_retries - 1:
                    # 指数退避：1s, 2s, 4s, 8s
                    await asyncio.sleep(1 * (2 ** attempt))
                    upstream_headers = await _build_headers_by_profile(profile, model)

            # 如果所有重试都是限流错误，使用最后一次的响应
            if is_rate_limit_status(r.status_code) and last_retry_response is not None:
                r = last_retry_response

        # 记录上下游响应（非流式）
        _dump_json(res_path, _resp_to_obj(r))

        if auth_type == "codex_oauth" and r.status_code < 400:
            try:
                codex_json = r.json()
                converted = _codex_responses_to_chat_completion(codex_json, model)
                _dump_json(res_path, {"status_code": r.status_code, "headers": dict(r.headers), "json": converted})
                return JSONResponse(content=converted, status_code=200)
            except Exception:
                # 转换失败时回退到透传，便于排查
                pass

        # ✅ 上游错误透传（状态码+body 原样返回）
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # ---- stream SSE (OpenAI SSE pass-through) ----
    async def sse_passthrough() -> AsyncIterator[bytes]:
        up_chunks: List[Any] = []
        try:
            if auth_type == "codex_oauth":
                # Codex responses SSE 事件模型与 chat.completions 不同，这里做兼容桥接：
                # 以上游非流式结果组装成 OpenAI SSE 两帧，保证客户端可消费。
                nonstream_body = dict(upstream_request_body)
                nonstream_body["stream"] = False
                async with httpx.AsyncClient(
                    verify=verify,
                    timeout=httpx.Timeout(timeout_seconds),
                    trust_env=trust_env,
                ) as client:
                    r = await client.post(upstream_url, headers=upstream_headers, json=nonstream_body)
                    up_chunks.append(
                        {
                            "type": "codex_stream_bridge_meta",
                            "status_code": r.status_code,
                            "headers": dict(r.headers),
                        }
                    )
                    if r.status_code >= 400:
                        err_text = r.text
                        up_chunks.append({"type": "error_body", "body": err_text})
                        error_data = {
                            "id": "chatcmpl-error",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                            "error": {"message": err_text, "type": "upstream_error", "code": r.status_code},
                        }
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

                    try:
                        codex_json = r.json()
                        converted = _codex_responses_to_chat_completion(codex_json, model)
                        content_text = (
                            converted.get("choices", [{}])[0].get("message", {}).get("content", "")
                            if isinstance(converted.get("choices"), list)
                            else ""
                        )
                        first_chunk = {
                            "id": str(converted.get("id") or f"chatcmpl-{uuid.uuid4().hex}"),
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {"content": content_text}, "finish_reason": None}],
                        }
                        last_chunk = {
                            "id": first_chunk["id"],
                            "object": "chat.completion.chunk",
                            "created": first_chunk["created"],
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": converted.get("usage"),
                        }
                        up_chunks.append({"type": "codex_stream_bridge_converted", "json": converted})
                        yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    except Exception as e:
                        up_chunks.append({"type": "convert_error", "error": str(e)})
                        error_data = {
                            "id": "chatcmpl-error",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                            "error": {"message": f"codex response convert error: {e}", "type": "convert_error"},
                        }
                        yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return

            async with httpx.AsyncClient(
                verify=verify,
                timeout=httpx.Timeout(timeout_seconds),
                trust_env=trust_env,
            ) as client:
                # 限流状态码重试逻辑：重试次数来自 profile 配置
                last_retry_err_text = None
                last_retry_status = None
                retry_headers = upstream_headers
                connection_established = False

                # 用于跟踪流式响应的数据
                has_valid_content = False
                content_buffer = ""

                for attempt in range(max_retries):
                    async with client.stream("POST", upstream_url, headers=retry_headers, json=body) as r:
                        meta = {
                            "type": "openai_passthrough_sse_meta",
                            "status_code": r.status_code,
                            "headers": dict(r.headers),
                        }
                        up_chunks.append(meta)

                        if is_rate_limit_status(r.status_code):
                            # 是限流错误，保存错误信息并关闭连接
                            err = await r.aread()
                            last_retry_err_text = err.decode("utf-8", errors="replace")
                            last_retry_status = r.status_code
                            up_chunks.append({"type": "error_body", "body": last_retry_err_text})
                            logging.warning(
                                f"{attempt} retryable response (chat/completions stream): {r.status_code} {last_retry_err_text}")
                            # 关闭连接，准备重试（进行下一次for循环）
                            if attempt < max_retries - 1:
                                # 指数退避
                                await asyncio.sleep(1 * (2 ** attempt))
                                retry_headers = await _build_headers_by_profile(profile, model)
                            continue

                        # 不是限流错误，继续在这个连接上处理
                        connection_established = True

                        # 处理其他错误（非406）
                        if r.status_code >= 400:
                            err = await r.aread()
                            err_text = err.decode("utf-8", errors="replace")
                            up_chunks.append({"type": "error_body", "body": err_text})
                            # 返回符合OpenAI格式的错误响应
                            error_data = {
                                "id": "chatcmpl-error",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": body.get("model", "unknown"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "error"
                                }]
                            }

                            # 如果是JSON格式的错误响应，尝试解析并包含详细信息
                            try:
                                error_json = json.loads(err_text)
                                if isinstance(error_json, dict):
                                    error_data["error"] = error_json
                            except:
                                # 如果不是有效的JSON，直接作为消息返回
                                error_data["error"] = {
                                    "message": err_text,
                                    "type": "upstream_error",
                                    "code": r.status_code
                                }

                            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return

                        # 正常情况，读取流数据
                        async for line in r.aiter_lines():
                            if not line:
                                continue

                            # 添加到 chunks 日志中
                            up_chunks.append(line)

                            # 检查是否是 SSE 数据行
                            if line.startswith("data:"):
                                data_part = line[5:].strip()  # 移除 "data:" 前缀

                                # 检查是否是结束标记
                                if data_part == "[DONE]":
                                    # 只有当我们收到了有效内容时才发送 DONE 标记
                                    if has_valid_content:
                                        yield b"data: [DONE]\n\n"
                                    break

                                # 尝试解析 JSON 数据
                                try:
                                    chunk_data = json.loads(data_part)

                                    # 检查是否有有效的内容
                                    choices = chunk_data.get("choices", [])
                                    if choices and len(choices) > 0:
                                        choice = choices[0]
                                        delta = choice.get("delta", {})
                                        content = delta.get("content")
                                        reasoning_content = delta.get("reasoning_content")
                                        reasoning = delta.get("reasoning")
                                        tool_calls = delta.get("tool_calls")
                                        finish_reason = choice.get("finish_reason")

                                        # 检查是否有任何有效内容（content 或 reasoning_content 或 tool_calls）
                                        # 分别处理每个字段，避免使用 elif 导致某些字段被忽略
                                        if content is not None and content != "":
                                            has_valid_content = True
                                            content_buffer += content
                                        elif content is not None and content == "" and len(content_buffer) > 0:
                                            # 空字符串但前面有内容，也认为是有效的
                                            has_valid_content = True
                                        if reasoning_content is not None and reasoning_content != "":
                                            has_valid_content = True
                                            content_buffer += reasoning_content
                                        if reasoning is not None and reasoning != "":
                                            has_valid_content = True
                                            content_buffer += reasoning
                                        if tool_calls is not None and len(tool_calls) > 0:
                                            has_valid_content = True

                                        # 如果有 finish_reason，也标记为有效
                                        if finish_reason is not None:
                                            has_valid_content = True

                                    # 适配CodeaAgent代码：只在有finish_reason时保留usage
                                    # 移除中间chunk的usage信息，避免被CodeAgent代码误判
                                    if "usage" in chunk_data:
                                        choices = chunk_data.get("choices", [])
                                        has_finish_reason = False
                                        if choices and len(choices) > 0:
                                            finish_reason = choices[0].get("finish_reason")
                                            if finish_reason is not None:
                                                has_finish_reason = True

                                        # 如果没有finish_reason，移除usage字段
                                        if not has_finish_reason:
                                            chunk_data.pop("usage", None)

                                    # 检查是否有工具调用完成
                                    should_emit_tool_calls = False
                                    if finish_reason == "tool_calls" and choices:
                                        # 检查是否有任何tool_calls（哪怕空数组）
                                        tool_calls_flat = []
                                        for choice in choices:
                                            if isinstance(choice, dict):
                                                delta = choice.get("delta", {})
                                                tcs = delta.get("tool_calls", [])
                                                if isinstance(tcs, list):
                                                    tool_calls_flat.extend(tcs)

                                        # 如果有tool_calls（哪怕是空数组）且之前已经有tool_calls记录，则触发返回
                                        if tool_calls_flat or has_valid_content:
                                            should_emit_tool_calls = True

                                    # 传递处理后的数据行
                                    yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n".encode("utf-8")

                                    # 如果检测到tool_calls完成，立即发送[i/]D[/i]标记并结束流
                                    if should_emit_tool_calls:
                                        # 确保所有tool calls都已经输出
                                        if has_valid_content:
                                            # 发送最终的有效内容块以触发tool calls处理
                                            final_chunk = {
                                                "id": "chatcmpl-final",
                                                "object": "chat.completion.chunk",
                                                "created": chunk_data.get("created", int(time.time())),
                                                "model": chunk_data.get("model", body.get("model", "unknown")),
                                                "choices": [{
                                                    "index": 0,
                                                    "delta": {"content": ""},
                                                    "finish_reason": "tool_calls"
                                                }]
                                            }
                                            yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode(
                                                "utf-8")

                                        # 立即发送DONE标记以结束流
                                        yield b"data: [DONE]\n\n"
                                        return
                                except json.JSONDecodeError:
                                    # 如果不是有效的 JSON，直接传递
                                    yield line.encode("utf-8") + b"\n\n"
                            else:
                                # 对于非数据行，直接传递
                                yield line.encode("utf-8") + b"\n"

                    # 如果已经成功建立连接且完成流式传输，则不再重试
                    if connection_established:
                        # 如果我们从未收到有效内容，添加一个最终的空内容块以防止客户端挂起
                        if not has_valid_content:
                            empty_chunk = {
                                "id": "chatcmpl-empty",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": body.get("model", "unknown"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": "stop"
                                }]
                            }
                            yield f"data: {json.dumps(empty_chunk, ensure_ascii=False)}\n\n".encode("utf-8")

                        # 确保发送 DONE 标记
                        yield b"data: [DONE]\n\n"
                        break

                    # 如果不是最后一次重试且连接失败（406/429），等待后继续
                    if not connection_established and attempt < max_retries - 1:
                        # 指数退避
                        await asyncio.sleep(1 * (2 ** attempt))
                        retry_headers = await _build_headers_by_profile(profile, model)

                # 如果所有重试都是限流错误，返回最后一次错误
                if not connection_established and last_retry_status is not None and is_rate_limit_status(
                        last_retry_status):
                    # 直接把错误原样吐回（客户端一般也能看到）
                    if last_retry_err_text is not None:
                        yield last_retry_err_text.encode("utf-8", errors="replace")
                    # 确保发送 DONE 标记
                    yield b"data: [DONE]\n\n"
                    return
        finally:
            # 无论正常/异常/客户端断开，尽最大努力落盘
            _dump_json(res_path, {"type": "openai_passthrough_sse_capture", "chunks": up_chunks})

    return StreamingResponse(sse_passthrough(), media_type="text/event-stream")
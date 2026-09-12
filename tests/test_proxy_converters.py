import json

import pytest

from proxy_converters import _build_codex_responses_payload_from_chat
from src.bridge.anthropic_codex import (
    anthropic_request_to_codex_payload,
    anthropic_request_to_openai_chat_body,
)


@pytest.mark.parametrize("strict", [None, False, True])
def test_chat_tools_preserve_strict_and_optional_parameters(strict):
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}, "target": {"type": "string"}},
        "required": ["action"],
        "additionalProperties": False,
    }
    function = {"name": "inbox", "parameters": schema}
    if strict is not None:
        function["strict"] = strict
    body = {
        "messages": [{"role": "user", "content": "Check inbox"}],
        "tools": [{"type": "function", "function": function}],
    }

    payload = _build_codex_responses_payload_from_chat(body, model="gpt-5.6-sol")

    assert payload["tools"][0]["strict"] is (strict if strict is not None else False)
    assert payload["tools"][0]["parameters"] == schema
    assert function.get("strict") is strict


def test_anthropic_tools_remain_nonstrict_when_converted_to_responses():
    payload = anthropic_request_to_codex_payload(
        model="gpt-5.6-sol",
        system=None,
        max_tokens=1024,
        stream=False,
        messages=[{"role": "user", "content": "Check inbox"}],
        tools=[
            {
                "name": "inbox",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "target": {"type": "string"},
                    },
                    "required": ["action"],
                },
            }
        ],
    )

    assert payload["tools"][0]["strict"] is False
    assert payload["tools"][0]["parameters"]["required"] == ["action"]


@pytest.mark.parametrize("image_only", [False, True])
def test_anthropic_user_images_reach_chat_and_codex_in_history(image_only):
    images = [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "aW1hZ2U=",
        }},
        {"type": "image", "source": {
            "type": "url", "url": "https://example.com/jd.png",
        }},
    ]
    content = images if image_only else [
        {"type": "text", "text": "前文"}, images[0],
        {"type": "text", "text": "后文"}, images[1],
    ]
    expected = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}},
        {"type": "image_url", "image_url": {"url": "https://example.com/jd.png"}},
    ]
    if not image_only:
        expected = [content[0], expected[0], content[2], expected[1]]
    kwargs = dict(
        model="gpt-5.6-terra", system=None, max_tokens=1024, stream=False,
        messages=[
            {"role": "user", "content": content},
            {"role": "assistant", "content": "收到"},
            {"role": "user", "content": [{"type": "text", "text": "刚才的图片是什么？"}]},
        ],
    )
    chat = anthropic_request_to_openai_chat_body(**kwargs)
    assert chat["messages"][0]["content"] == expected
    assert chat["messages"][2]["content"] == "刚才的图片是什么？"
    payload = anthropic_request_to_codex_payload(**kwargs)
    assert payload["input"][0]["content"] == [
        {"type": "input_image", "image_url": part["image_url"]["url"]}
        if part["type"] == "image_url" else {"type": "input_text", "text": part["text"]}
        for part in expected
    ]


def test_tool_content_with_image_url_is_preserved_in_function_call_output():
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_img_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{\"path\":\"/tmp/a.png\"}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_img_1",
                "content": [
                    {"type": "text", "text": "Image metadata"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            },
        ],
        "stream": False,
    }

    payload = _build_codex_responses_payload_from_chat(body, model="gpt-5.2-codex")
    input_items = payload["input"]
    outputs = [x for x in input_items if isinstance(x, dict) and x.get("type") == "function_call_output"]
    assert len(outputs) == 1
    output_value = outputs[0]["output"]
    assert isinstance(output_value, list)
    assert output_value[0]["type"] == "input_text"
    assert output_value[1]["type"] == "input_image"
    assert output_value[1]["image_url"].startswith("data:image/png;base64,")


def test_tool_content_json_string_with_output_content_is_normalized():
    raw_tool_content = {
        "call_id": "call_img_2",
        "name": "read",
        "output": {
            "content": [
                {"type": "text", "text": "Image metadata"},
                {"type": "image_url", "image_url": "data:image/png;base64,BBB"},
            ]
        },
    }
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_img_2",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{\"path\":\"/tmp/b.png\"}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_img_2",
                "content": json.dumps(raw_tool_content, ensure_ascii=False),
            },
        ],
        "stream": False,
    }

    payload = _build_codex_responses_payload_from_chat(body, model="gpt-5.2-codex")
    input_items = payload["input"]
    outputs = [x for x in input_items if isinstance(x, dict) and x.get("type") == "function_call_output"]
    assert len(outputs) == 1
    output_value = outputs[0]["output"]
    assert isinstance(output_value, list)
    assert output_value[0] == {"type": "input_text", "text": "Image metadata"}
    assert output_value[1] == {"type": "input_image", "image_url": "data:image/png;base64,BBB"}

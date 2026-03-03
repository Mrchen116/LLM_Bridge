import json

from proxy_converters import _build_codex_responses_payload_from_chat


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

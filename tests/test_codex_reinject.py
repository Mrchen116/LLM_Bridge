from fastapi.testclient import TestClient

from tests.support import FakeAsyncClient, FakeStreamResponse


def test_messages_codex_oauth_should_reinject_encrypted_content_when_context_exact_match_after_trailing_user_suffix_removal(
    client: TestClient,
):
    """测试上下文严格命中后回填 encrypted_content 且顺序正确。"""
    # 第 1 轮：上游返回 reasoning.encrypted_content，代理应缓存。
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_turn_1","output":[{"type":"reasoning","encrypted_content":"enc_turn_1","summary":[{"type":"summary_text","text":"S1"}]},{"type":"message","content":[{"type":"output_text","text":"A1"}]}],"usage":{"input_tokens":5,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    turn1_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "system": "You are Codex. Follow repo rules strictly.",
        "messages": [{"role": "user", "content": "Q1"}],
        "max_tokens": 128,
        "metadata": {"user_id": "user_x_session_reinjectspec"},
    }
    turn1 = client.post("/v1/messages", json=turn1_payload)
    assert turn1.status_code == 200

    # 第 2 轮：messages 为 [user1, assistant1, user2]。
    # 去掉末尾连续 user 后缀后，剩余上下文应与第 1 轮结束时上下文严格一致，期望触发 encrypted_content 回填。
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_turn_2","output":[{"type":"message","content":[{"type":"output_text","text":"A2"}]}],"usage":{"input_tokens":7,"output_tokens":4}}}',
            "data: [DONE]",
        ],
    )
    turn2_payload = {
        "model": "codexOAuth:gpt-5.2-codex",
        "system": "You are Codex. Follow repo rules strictly.",
        "messages": [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
            {"role": "user", "content": "Q2"},
        ],
        "max_tokens": 128,
        "metadata": {"user_id": "user_x_session_reinjectspec"},
    }
    turn2 = client.post("/v1/messages", json=turn2_payload)
    assert turn2.status_code == 200

    up2 = FakeAsyncClient.last_stream_args["json"]
    assert any(
        isinstance(item, dict)
        and item.get("role") == "system"
        and item.get("content") == "You are Codex. Follow repo rules strictly."
        for item in up2.get("input", [])
    ), "system prompt 应进入 codex input，并参与上下文严格匹配"
    input_items = up2.get("input", [])

    def _find_index(pred):
        for i, item in enumerate(input_items):
            if pred(item):
                return i
        return -1

    idx_user_q1 = _find_index(
        lambda item: isinstance(item, dict)
        and item.get("role") == "user"
        and isinstance(item.get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "input_text" and p.get("text") == "Q1"
            for p in item.get("content", [])
        )
    )
    idx_reasoning = _find_index(
        lambda item: isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_turn_1"
    )
    idx_assistant_a1 = _find_index(
        lambda item: isinstance(item, dict)
        and item.get("role") == "assistant"
        and isinstance(item.get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "output_text" and p.get("text") == "A1"
            for p in item.get("content", [])
        )
    )
    idx_user_q2 = _find_index(
        lambda item: isinstance(item, dict)
        and item.get("role") == "user"
        and isinstance(item.get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "input_text" and p.get("text") == "Q2"
            for p in item.get("content", [])
        )
    )

    assert idx_reasoning != -1, "命中严格上下文后，应回填 encrypted_content"
    assert (
        idx_user_q1 != -1 and idx_assistant_a1 != -1 and idx_user_q2 != -1
    ), "测试前提错误：未找到 Q1/A1/Q2 对应输入项"
    assert input_items[idx_reasoning].get("summary") == [{"type": "summary_text", "text": "S1"}], (
        "回填 reasoning item 时应保留上游返回的 summary 字段"
    )
    assert idx_user_q1 < idx_reasoning < idx_assistant_a1 < idx_user_q2, (
        "回填顺序必须为：第一轮 user -> encrypted reasoning -> 第一轮 assistant -> 下一轮 user"
    )


def test_messages_codex_oauth_should_not_reinject_encrypted_content_when_model_diff_even_if_context_match(
    client: TestClient,
):
    """测试上下文一致但模型不同场景下禁止回填 encrypted_content。"""
    # 第 1 轮：缓存 encrypted_content（模型 gpt-5.2-codex）
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_model_1","output":[{"type":"reasoning","encrypted_content":"enc_model_1"},{"type":"message","content":[{"type":"output_text","text":"A1"}]}],"usage":{"input_tokens":5,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    turn1 = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.2-codex",
            "system": "You are Codex. Follow repo rules strictly.",
            "messages": [{"role": "user", "content": "Q1"}],
            "metadata": {"user_id": "user_x_session_modelguard"},
        },
    )
    assert turn1.status_code == 200

    # 第 2 轮：上下文保持一致，但模型换成 gpt-5.1，期望严格 miss，不回填。
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_model_2","output":[{"type":"message","content":[{"type":"output_text","text":"A2"}]}],"usage":{"input_tokens":7,"output_tokens":4}}}',
            "data: [DONE]",
        ],
    )
    turn2 = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.1",
            "system": "You are Codex. Follow repo rules strictly.",
            "messages": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
                {"role": "user", "content": "Q2"},
            ],
            "metadata": {"user_id": "user_x_session_modelguard"},
        },
    )
    assert turn2.status_code == 200

    up2 = FakeAsyncClient.last_stream_args["json"]
    assert not any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_model_1"
        for item in up2.get("input", [])
    ), "即使上下文一致，model 不同也不允许回填 encrypted_content"


def test_messages_codex_oauth_should_not_reinject_encrypted_content_when_system_diff_even_if_messages_match(
    client: TestClient,
):
    """测试 messages 一致但 system 不同场景下禁止回填 encrypted_content。"""
    # 第 1 轮：缓存 encrypted_content（system=A）
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_sys_1","output":[{"type":"reasoning","encrypted_content":"enc_sys_1"},{"type":"message","content":[{"type":"output_text","text":"A1"}]}],"usage":{"input_tokens":5,"output_tokens":3}}}',
            "data: [DONE]",
        ],
    )
    turn1 = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.2-codex",
            "system": "System A",
            "messages": [{"role": "user", "content": "Q1"}],
            "metadata": {"user_id": "user_x_session_systemguard"},
        },
    )
    assert turn1.status_code == 200

    # 第 2 轮：messages 保持一致但 system 改为 B，期望严格 miss。
    FakeAsyncClient.stream_response = FakeStreamResponse(
        status_code=200,
        lines=[
            'data: {"type":"response.completed","response":{"id":"resp_sys_2","output":[{"type":"message","content":[{"type":"output_text","text":"A2"}]}],"usage":{"input_tokens":7,"output_tokens":4}}}',
            "data: [DONE]",
        ],
    )
    turn2 = client.post(
        "/v1/messages",
        json={
            "model": "codexOAuth:gpt-5.2-codex",
            "system": "System B",
            "messages": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
                {"role": "user", "content": "Q2"},
            ],
            "metadata": {"user_id": "user_x_session_systemguard"},
        },
    )
    assert turn2.status_code == 200

    up2 = FakeAsyncClient.last_stream_args["json"]
    assert not any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content") == "enc_sys_1"
        for item in up2.get("input", [])
    ), "messages 一致但 system 不一致时，也不允许回填 encrypted_content"

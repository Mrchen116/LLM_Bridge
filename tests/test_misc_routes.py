from fastapi.testclient import TestClient


def test_health_ok(client: TestClient):
    """测试健康检查接口返回可用状态。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_count_tokens_invalid_json(client: TestClient):
    """测试 count_tokens 在非法 JSON 请求体下返回参数错误。"""
    resp = client.post(
        "/v1/messages/count_tokens",
        content="not-json",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_session_stats_not_found(client: TestClient):
    """测试查询不存在会话统计时返回 404。"""
    resp = client.get("/session/not-exist/stats")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_count_tokens_supports_openai_chat_format(client: TestClient):
    """测试 count_tokens 支持 openai_chat 格式。"""
    payload = {
        "format": "openai_chat",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ],
    }
    resp = client.post("/v1/messages/count_tokens", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "openai_chat"
    assert body["input_tokens"] > 0


def test_count_tokens_supports_openai_responses_format(client: TestClient):
    """测试 count_tokens 支持 openai_responses 格式。"""
    payload = {
        "format": "openai_responses",
        "instructions": "You are helpful.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello responses"}]}],
    }
    resp = client.post("/v1/messages/count_tokens", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["format"] == "openai_responses"
    assert body["input_tokens"] > 0


def test_session_stats_aggregates_three_formats(client: TestClient):
    """测试 session stats 可聚合三种格式日志。"""
    import json
    from pathlib import Path

    session_id = "fmtmix"
    session_dir = Path.cwd() / "logs" / "session" / f"2026-02-16_15-00-00_000_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    (session_dir / "2026-02-16_15-00-00_001-non-stream-res-anthropic_messages.json").write_text(
        json.dumps({"usage": {"input_tokens": 2, "output_tokens": 3}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (session_dir / "2026-02-16_15-00-00_002-non-stream-res-openai_chat.json").write_text(
        json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 7}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (session_dir / "2026-02-16_15-00-00_003-non-stream-res-openai_responses.json").write_text(
        json.dumps({"usage": {"input_tokens": 11, "output_tokens": 13}}, ensure_ascii=False),
        encoding="utf-8",
    )

    resp = client.get(f"/session/{session_id}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["input_tokens"] == 18
    assert body["output_tokens"] == 23
    assert body["num_turns"] == 3
    assert body["by_format"]["anthropic_messages"]["input_tokens"] == 2
    assert body["by_format"]["openai_chat"]["input_tokens"] == 5
    assert body["by_format"]["openai_responses"]["input_tokens"] == 11

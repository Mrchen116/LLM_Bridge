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

from src.adapters.upstream_executor import (
    CODEX_DEFAULT_VERSION,
    build_codex_oauth_style_headers,
)


def test_codex_oauth_headers_use_current_default_version(monkeypatch):
    monkeypatch.delenv("CODEX_UPSTREAM_CLIENT_VERSION", raising=False)

    headers = build_codex_oauth_style_headers(
        auth_headers={},
        client_headers={},
        session_id=None,
    )

    assert CODEX_DEFAULT_VERSION == "0.144.6"
    assert headers["version"] == "0.144.6"
    assert headers["user-agent"] == "codex_cli_rs/0.144.6 (LLM_PROXY)"


def test_codex_oauth_headers_allow_version_override(monkeypatch):
    monkeypatch.setenv("CODEX_UPSTREAM_CLIENT_VERSION", "0.145.1")

    headers = build_codex_oauth_style_headers(
        auth_headers={},
        client_headers={},
        session_id=None,
    )

    assert headers["version"] == "0.145.1"
    assert headers["user-agent"] == "codex_cli_rs/0.145.1 (LLM_PROXY)"


def test_codex_oauth_headers_prefer_downstream_version(monkeypatch):
    monkeypatch.setenv("CODEX_UPSTREAM_CLIENT_VERSION", "0.145.1")

    headers = build_codex_oauth_style_headers(
        auth_headers={},
        client_headers={"version": "0.146.0"},
        session_id=None,
    )

    assert headers["version"] == "0.146.0"
    assert headers["user-agent"] == "codex_cli_rs/0.146.0 (LLM_PROXY)"

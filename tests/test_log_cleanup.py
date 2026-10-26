import asyncio
import errno
import threading
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import proxy_logging
from src.observability import log_cleanup
from src.observability.log_cleanup import (
    RequestLogMiddleware,
    cleanup_logs,
    log_cleanup_worker,
    request_log_scope,
    track_turn,
)
from tests.support import FakeAsyncClient, FakeStreamResponse


NOW = datetime(2026, 10, 25, 12)


def timestamp(date):
    return date.strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]


def turn_files(tmp_path, date, session="2026-01-01_00-00-00_000_long-lived"):
    stamp = timestamp(date)
    paths = [
        tmp_path / "raw" / "openai_codex" / f"{stamp}-req-anthropic_messages.json",
        tmp_path / "raw" / "openai_codex" / f"{stamp}-upstream-res-anthropic_messages.json",
        tmp_path / "session" / session / f"{stamp}-req-anthropic_messages.json",
        tmp_path / "session" / session / f"{stamp}-downstream-res-anthropic_messages.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    return paths


def clean(tmp_path, **kwargs):
    return cleanup_logs(str(tmp_path / "raw"), str(tmp_path / "session"), now=NOW, **kwargs)


def test_expiration_keeps_new_turns_in_old_session(tmp_path):
    old = turn_files(tmp_path, NOW - timedelta(days=8))
    boundary = turn_files(tmp_path, NOW - timedelta(days=7))
    recent = turn_files(tmp_path, NOW - timedelta(hours=1))

    result = clean(tmp_path)

    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in boundary + recent)
    assert result == {"removed_files": 4, "freed_bytes": 40, "remaining_bytes": 80}


def test_shared_cap_removes_whole_oldest_turn_without_minimum_counts(tmp_path):
    old = turn_files(tmp_path, NOW - timedelta(hours=3))
    middle = turn_files(tmp_path, NOW - timedelta(hours=2))
    new = turn_files(tmp_path, NOW - timedelta(hours=1))

    assert clean(tmp_path, max_bytes=80)["remaining_bytes"] == 80
    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in middle + new)


def test_active_turn_survives_age_and_size_limits_then_becomes_eligible(tmp_path):
    date = NOW - timedelta(days=9)
    active = turn_files(tmp_path, date)
    inactive = turn_files(tmp_path, NOW - timedelta(hours=1))
    with request_log_scope():
        track_turn(str(tmp_path / "raw"), str(tmp_path / "session"), timestamp(date))
        track_turn(str(tmp_path / "raw"), str(tmp_path / "session"), timestamp(date))
        assert clean(tmp_path, max_bytes=0)["remaining_bytes"] == 40
        assert all(path.exists() for path in active)
        assert not any(path.exists() for path in inactive)
    assert clean(tmp_path, max_bytes=0)["remaining_bytes"] == 0
    assert not any(path.exists() for path in active)


def test_cleanup_ignores_symlinks_and_unmanaged_files(tmp_path):
    files = turn_files(tmp_path, NOW - timedelta(days=8))
    unrelated = tmp_path / "raw" / "openai_codex" / "config.json"
    unrelated.write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / files[0].name
    target.write_text("keep")
    (tmp_path / "raw" / "linked-bucket").symlink_to(outside, target_is_directory=True)
    linked_file = files[0].parent / f"{timestamp(NOW - timedelta(days=10))}-req-openai_chat.json"
    linked_file.symlink_to(target)

    clean(tmp_path, max_bytes=0)

    assert unrelated.read_text() == "keep"
    assert target.read_text() == "keep"
    assert linked_file.is_symlink()
    assert not any(path.exists() for path in files)


@pytest.mark.parametrize("cancel", [False, True])
def test_asgi_protection_lasts_through_stream_and_is_released_on_cancel(tmp_path, monkeypatch, cancel):
    monkeypatch.setattr(proxy_logging, "REQUEST_LOGGING_ENABLED", True)

    async def scenario():
        date = NOW - timedelta(days=8)
        files = turn_files(tmp_path, date)
        streaming = asyncio.Event()
        finish = asyncio.Event()

        async def handler(scope, receive, send):
            track_turn(str(tmp_path / "raw"), str(tmp_path / "session"), timestamp(date))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            streaming.set()
            await finish.wait()
            await send({"type": "http.response.body", "body": b"last", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            assert all(path.exists() for path in files)

        task = asyncio.create_task(RequestLogMiddleware(handler)({"type": "http"}, receive, send))
        await streaming.wait()
        assert clean(tmp_path, max_bytes=0)["removed_files"] == 0
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            finish.set()
            await task
        assert clean(tmp_path, max_bytes=0)["removed_files"] == 4

    asyncio.run(scenario())


def test_worker_runs_without_requests_and_recovers_from_scan_error(monkeypatch):
    monkeypatch.setattr(proxy_logging, "REQUEST_LOGGING_ENABLED", True)
    called_twice = threading.Event()
    calls = []

    def cleanup(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("temporary scan failure")
        called_twice.set()

    monkeypatch.setattr(log_cleanup, "cleanup_logs", cleanup)
    with log_cleanup_worker("raw", "session", interval=0.01):
        assert called_twice.wait(timeout=2)
    assert len(calls) >= 2
    assert not any(t.name == "log-cleanup" and t.is_alive() for t in threading.enumerate())


def test_disabled_logging_does_not_start_cleanup_or_remove_old_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_logging, "REQUEST_LOGGING_ENABLED", False)
    old = turn_files(tmp_path, NOW - timedelta(days=8))

    with log_cleanup_worker(str(tmp_path / "raw"), str(tmp_path / "session")):
        assert not any(t.name == "log-cleanup" and t.is_alive() for t in threading.enumerate())

    assert all(path.exists() for path in old)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["directory", "open", "write"])
def test_log_io_failure_does_not_fail_model_request(client_with_logs, monkeypatch, caplog, stream, failure):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    if failure == "directory":
        monkeypatch.setattr(proxy_logging.os, "makedirs", full)
    elif failure == "open":
        monkeypatch.setattr(proxy_logging, "open", full, raising=False)
    else:
        monkeypatch.setattr(proxy_logging.json, "dump", full)
    FakeAsyncClient.post_response = httpx.Response(200, json={
        "id": "chatcmpl_log_failure", "model": "kimi-k2.5",
        "choices": [{"message": {"role": "assistant", "content": "still works"}, "finish_reason": "stop"}],
    })
    FakeAsyncClient.stream_response = FakeStreamResponse(lines=[
        'data: {"id":"chatcmpl_log_failure","choices":[{"delta":{"content":"still works"},"finish_reason":null}]}',
        'data: {"id":"chatcmpl_log_failure","choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ])
    response = client_with_logs.post("/v1/messages", json={
        "model": "moonshot:kimi-k2.5", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": "session_log_failure"},
    })

    assert response.status_code == 200
    assert "still works" in response.text
    assert "No space left on device" in caplog.text

"""FastAPI integration tests against a mocked controller."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ts6_stream_bot.api.app import create_app
from ts6_stream_bot.pipeline import SourceOpenError, StreamController, StreamState
from ts6_stream_bot.pipeline.controller import StreamStatus


@pytest.fixture
def mock_controller():
    mock = AsyncMock(spec=StreamController)
    mock.startup = AsyncMock()
    mock.shutdown = AsyncMock()
    mock.status = AsyncMock(
        return_value=StreamStatus(state=StreamState.IDLE, room="default")
    )
    mock.play = AsyncMock(
        return_value=StreamStatus(
            state=StreamState.PLAYING,
            room="default",
            url="https://www.youtube.com/watch?v=test",
            title="Test",
            source_class="YoutubeSource",
            stream_path="/stream/default/index.m3u8",
        )
    )
    mock.pause = AsyncMock(
        return_value=StreamStatus(state=StreamState.PAUSED, room="default")
    )
    mock.resume = AsyncMock(
        return_value=StreamStatus(state=StreamState.PLAYING, room="default")
    )
    mock.seek = AsyncMock(
        return_value=StreamStatus(state=StreamState.PLAYING, room="default")
    )
    mock.stop = AsyncMock(
        return_value=StreamStatus(state=StreamState.IDLE, room="default")
    )
    mock.screenshot = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def client(monkeypatch, mock_controller):
    """TestClient against an app whose lifespan instantiates our mock."""
    monkeypatch.setattr(
        "ts6_stream_bot.api.app.StreamController", lambda: mock_controller
    )
    app = create_app()
    with TestClient(app) as tc:
        yield tc


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body


def test_status_does_not_require_auth(client) -> None:
    r = client.get("/status")
    assert r.status_code == 200
    assert r.json()["state"] == "idle"


def test_play_requires_api_key(client) -> None:
    r = client.post("/play", json={"url": "https://www.youtube.com/watch?v=x"})
    assert r.status_code == 401


def test_play_with_api_key(client) -> None:
    r = client.post(
        "/play",
        json={"url": "https://www.youtube.com/watch?v=x"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "playing"
    assert body["source_class"] == "YoutubeSource"


def test_play_source_open_failure_returns_502(client, mock_controller) -> None:
    mock_controller.play = AsyncMock(side_effect=SourceOpenError("ffmpeg crashed"))
    r = client.post(
        "/play",
        json={"url": "https://www.youtube.com/watch?v=x"},
        headers={"X-API-Key": "test-key"},
    )
    assert r.status_code == 502
    body = r.json()
    assert body["error"] == "source_open_failed"
    assert "ffmpeg crashed" in body["detail"]


def test_invalid_payload(client) -> None:
    r = client.post("/play", json={}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 422


def test_metrics_endpoint(client) -> None:
    r = client.get("/metrics")
    assert r.status_code == 200
    # Either real prometheus output or the no-op shim - both are fine.
    body = r.text
    assert (
        "ts6_stream_bot_play_requests_total" in body
        or "prometheus_client not installed" in body
    )


def test_play_increments_metrics(client) -> None:
    pytest.importorskip("prometheus_client")
    client.post(
        "/play",
        json={"url": "https://www.youtube.com/watch?v=x"},
        headers={"X-API-Key": "test-key"},
    )
    r = client.get("/metrics")
    assert "ts6_stream_bot_play_requests_total" in r.text


def test_screenshot_requires_api_key(client) -> None:
    r = client.get("/debug/screenshot")
    assert r.status_code == 401


def test_screenshot_409_when_idle(client) -> None:
    r = client.get("/debug/screenshot", headers={"X-API-Key": "test-key"})
    assert r.status_code == 409


def test_screenshot_returns_png(client, mock_controller) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    mock_controller.screenshot = AsyncMock(return_value=png)
    r = client.get("/debug/screenshot", headers={"X-API-Key": "test-key"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == png

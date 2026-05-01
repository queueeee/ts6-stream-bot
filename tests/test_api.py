"""FastAPI integration tests against a mocked controller."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ts6_stream_bot.api.app import create_app
from ts6_stream_bot.pipeline import StreamController, StreamState
from ts6_stream_bot.pipeline.controller import StreamStatus


@pytest.fixture
def client(monkeypatch):
    """TestClient against an app with a fully-mocked controller."""
    app = create_app()

    # Replace lifespan controller with a mock
    mock = AsyncMock(spec=StreamController)
    mock.startup = AsyncMock()
    mock.shutdown = AsyncMock()
    mock.status = AsyncMock(return_value=StreamStatus(state=StreamState.IDLE, room="default"))
    mock.play = AsyncMock(return_value=StreamStatus(
        state=StreamState.PLAYING,
        room="default",
        url="https://www.youtube.com/watch?v=test",
        title="Test",
        source_class="YoutubeSource",
        stream_path="/stream/default/index.m3u8",
    ))
    mock.pause = AsyncMock(return_value=StreamStatus(state=StreamState.PAUSED, room="default"))
    mock.resume = AsyncMock(return_value=StreamStatus(state=StreamState.PLAYING, room="default"))
    mock.seek = AsyncMock(return_value=StreamStatus(state=StreamState.PLAYING, room="default"))
    mock.stop = AsyncMock(return_value=StreamStatus(state=StreamState.IDLE, room="default"))

    # Bypass real lifespan by setting state manually before TestClient enters context
    with TestClient(app) as tc:
        tc.app.state.controller = mock
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


def test_invalid_payload(client) -> None:
    r = client.post("/play", json={}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 422

"""StreamController orchestrates the lifecycle:

    idle  --play(url)-->  loading  --source ready-->  playing
    playing  <--pause/resume-->  paused
    {playing, paused, loading}  --stop-->  idle

A single instance lives at app startup and serves all API requests.
State transitions are guarded by an asyncio.Lock so concurrent requests can't race.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from ts6_stream_bot.config import settings
from ts6_stream_bot.pipeline.browser import BrowserManager
from ts6_stream_bot.pipeline.capture import HlsCapture
from ts6_stream_bot.sources import StreamSource, resolve_source

log = structlog.get_logger(__name__)


class SourceOpenError(Exception):
    """Raised when a source fails to open or start the capture pipeline.

    Carries the underlying failure so callers can surface it to the user
    (translated to HTTP 502 by the API layer).
    """


class StreamState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class StreamStatus:
    state: StreamState
    room: str
    url: str | None = None
    title: str | None = None
    source_class: str | None = None
    error: str | None = None
    stream_path: str | None = None  # nginx-relative path if a capture is running
    extras: dict[str, str] = field(default_factory=dict)


class StreamController:
    """Singleton controller orchestrating browser, capture, and source lifecycle."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = StreamState.IDLE
        self._browser = BrowserManager()
        self._capture: HlsCapture | None = None
        self._source: StreamSource | None = None
        self._url: str | None = None
        self._error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        """Call once on app startup."""
        await self._browser.start()
        log.info("controller.ready")

    async def shutdown(self) -> None:
        """Call once on app shutdown."""
        async with self._lock:
            await self._teardown_locked()
            await self._browser.stop()

    # --- public API --------------------------------------------------------

    async def play(self, url: str, room: str | None = None) -> StreamStatus:
        room = room or settings.DEFAULT_ROOM
        async with self._lock:
            await self._teardown_locked()
            self._state = StreamState.LOADING
            self._url = url
            self._error = None

            source_cls = resolve_source(url)
            log.info("controller.source_resolved", url=url, source=source_cls.__name__)
            source = source_cls()

            try:
                await source.open(self._browser.context, url)
                # Start capture before play(): we want the first frame in the stream
                self._capture = HlsCapture(room=room)
                await self._capture.start()
                await source.play()
            except Exception as exc:
                log.exception("controller.play_failed", error=str(exc))
                self._error = str(exc)
                self._state = StreamState.IDLE
                # Best-effort cleanup
                with suppress(Exception):
                    await source.close()
                if self._capture is not None:
                    await self._capture.stop()
                    self._capture = None
                raise SourceOpenError(str(exc)) from exc

            self._source = source
            self._state = StreamState.PLAYING
            return self._status_locked(room)

    async def pause(self) -> StreamStatus:
        async with self._lock:
            if self._state == StreamState.PLAYING and self._source is not None:
                await self._source.pause()
                self._state = StreamState.PAUSED
            return self._status_locked()

    async def resume(self) -> StreamStatus:
        async with self._lock:
            if self._state == StreamState.PAUSED and self._source is not None:
                await self._source.play()
                self._state = StreamState.PLAYING
            return self._status_locked()

    async def seek(self, seconds: int) -> StreamStatus:
        async with self._lock:
            if self._source is not None:
                await self._source.seek(seconds)
            return self._status_locked()

    async def stop(self) -> StreamStatus:
        async with self._lock:
            await self._teardown_locked()
            return self._status_locked()

    async def status(self) -> StreamStatus:
        async with self._lock:
            return self._status_locked()

    async def screenshot(self) -> bytes | None:
        """Return a PNG screenshot of the active page, or None if no source is open."""
        async with self._lock:
            if self._source is None or self._source.page is None:
                return None
            png: bytes = await self._source.page.screenshot(type="png", full_page=False)
            return png

    # --- internals ---------------------------------------------------------

    async def _teardown_locked(self) -> None:
        """Tear down source + capture. Caller must hold self._lock."""
        if self._source is not None:
            try:
                await self._source.close()
            except Exception as e:
                log.warning("controller.source_close_failed", error=str(e))
            self._source = None
        if self._capture is not None:
            await self._capture.stop()
            self._capture = None
        self._url = None
        self._state = StreamState.IDLE

    def _status_locked(self, room: str | None = None) -> StreamStatus:
        room = room or settings.DEFAULT_ROOM
        return StreamStatus(
            state=self._state,
            room=room,
            url=self._url,
            title=self._source.title() if self._source else None,
            source_class=type(self._source).__name__ if self._source else None,
            error=self._error,
            stream_path=self._capture.stream_url_path() if self._capture else None,
        )

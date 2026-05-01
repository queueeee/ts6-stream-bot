"""StreamController orchestrates the lifecycle:

    idle  --play(url)-->  loading  --source ready-->  playing
    playing  <--pause/resume-->  paused
    {playing, paused, loading}  --stop-->  idle

A single instance lives at app startup and serves all API requests.
State transitions are guarded by an asyncio.Lock so concurrent requests can't race.

Phase 0 status: the HLS capture pipeline has been removed. The controller still
opens browser sources and drives play/pause/seek, but produces no output. The
TS6 voice client (audio) and aiortc WebRTC video sender (video) are added in
phases 1-3.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

import structlog

from ts6_stream_bot.pipeline.browser import BrowserManager
from ts6_stream_bot.sources import StreamSource, resolve_source

log = structlog.get_logger(__name__)


class SourceOpenError(Exception):
    """Raised when a source fails to open. Surfaced as HTTP 502 by the API layer."""


class StreamState(StrEnum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class StreamStatus:
    state: StreamState
    url: str | None = None
    title: str | None = None
    source_class: str | None = None
    error: str | None = None


class StreamController:
    """Singleton controller orchestrating browser + source lifecycle."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = StreamState.IDLE
        self._browser = BrowserManager()
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

    async def play(self, url: str) -> StreamStatus:
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
                await source.play()
            except Exception as exc:
                log.exception("controller.play_failed", error=str(exc))
                self._error = str(exc)
                self._state = StreamState.IDLE
                with suppress(Exception):
                    await source.close()
                raise SourceOpenError(str(exc)) from exc

            self._source = source
            self._state = StreamState.PLAYING
            return self._status_locked()

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
        """Tear down the active source. Caller must hold self._lock."""
        if self._source is not None:
            try:
                await self._source.close()
            except Exception as e:
                log.warning("controller.source_close_failed", error=str(e))
            self._source = None
        self._url = None
        self._state = StreamState.IDLE

    def _status_locked(self) -> StreamStatus:
        return StreamStatus(
            state=self._state,
            url=self._url,
            title=self._source.title() if self._source else None,
            source_class=type(self._source).__name__ if self._source else None,
            error=self._error,
        )

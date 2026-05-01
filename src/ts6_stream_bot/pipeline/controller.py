"""StreamController orchestrates the lifecycle:

    idle  --play(url)-->  loading  --source ready-->  playing
    playing  <--pause/resume-->  paused
    {playing, paused, loading}  --stop-->  idle

A single instance lives at app startup and serves all API requests.
State transitions are guarded by an asyncio.Lock so concurrent requests
can't race.

Phase 4 wires together:

* ``BrowserManager`` (Playwright headful Chromium in Xvfb)
* ``Ts3Client`` (UDP voice client to the TS6 server)
* ``StreamSignaling`` (TS6 stream protocol)
* ``VideoCapture`` (x11grab + Pulse -> aiortc tracks)
* ``StreamPublisher`` (one RTCPeerConnection per joined viewer)

Lifecycle: ``startup()`` brings up the browser + TS6 connection +
allocates one stream. The stream stays alive across ``play()``/``stop()``
so viewers in the channel don't have to re-join on every URL change.
``shutdown()`` deallocates the stream and disconnects.

If ``settings.TS6_HOST`` is empty (e.g. local development with no
server reachable), the controller still works for the source-rendering
half - it just doesn't push anything to TS6 and ``status.streaming``
stays ``False``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum

import structlog

from ts6_stream_bot.config import settings
from ts6_stream_bot.pipeline.browser import BrowserManager
from ts6_stream_bot.pipeline.stream_publisher import StreamPublisher
from ts6_stream_bot.pipeline.stream_signaling import StreamSignaling
from ts6_stream_bot.pipeline.video_capture import VideoCapture, VideoCaptureConfig
from ts6_stream_bot.sources import StreamSource, resolve_source
from ts6_stream_bot.ts3lib.client import Ts3Client, Ts3ClientOptions
from ts6_stream_bot.ts3lib.identity_store import load_or_generate_identity

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

    # TS6 surface
    ts6_connected: bool = False
    ts6_client_id: int | None = None
    streaming: bool = False
    stream_id: str | None = None
    viewer_count: int = 0


class StreamController:
    """Singleton controller orchestrating browser + TS6 client + stream."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state = StreamState.IDLE
        self._browser = BrowserManager()
        self._source: StreamSource | None = None
        self._url: str | None = None
        self._error: str | None = None

        # TS6 + WebRTC stack. Built in startup() so we have a running event loop.
        self._ts3_client: Ts3Client | None = None
        self._signaling: StreamSignaling | None = None
        self._capture: VideoCapture | None = None
        self._publisher: StreamPublisher | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        """Launch browser + (if configured) connect to TS6 + allocate the stream."""
        await self._browser.start()
        log.info("controller.browser_ready")

        if not settings.TS6_HOST:
            log.warning("controller.ts6_host_not_set", note="bot runs without TS6 output")
            return

        try:
            await self._connect_ts6()
        except Exception as exc:
            log.exception("controller.ts6_connect_failed", error=str(exc))
            # Don't fail startup - the bot is still useful for source debugging
            # and the operator can fix TS6 settings + restart.
            return

        try:
            await self._allocate_stream()
        except Exception as exc:
            log.exception("controller.stream_allocate_failed", error=str(exc))

        log.info("controller.ready")

    async def shutdown(self) -> None:
        """Tear everything down. Best-effort; we never raise here."""
        async with self._lock:
            await self._teardown_source_locked()

        if self._publisher is not None:
            with suppress(Exception):
                await self._publisher.stop()
            self._publisher = None
        self._capture = None
        self._signaling = None

        if self._ts3_client is not None:
            with suppress(Exception):
                self._ts3_client.disconnect()
            # Give the disconnect packet a moment to leave.
            await asyncio.sleep(0.6)
            with suppress(Exception):
                self._ts3_client.force_close()
            self._ts3_client = None

        with suppress(Exception):
            await self._browser.stop()

    # --- public API --------------------------------------------------------

    async def play(self, url: str) -> StreamStatus:
        async with self._lock:
            await self._teardown_source_locked()
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
        """Stop the active source. Stream allocation + TS6 connection stay
        up so viewers don't get kicked out of the channel."""
        async with self._lock:
            await self._teardown_source_locked()
            return self._status_locked()

    async def status(self) -> StreamStatus:
        async with self._lock:
            return self._status_locked()

    async def screenshot(self) -> bytes | None:
        async with self._lock:
            if self._source is None or self._source.page is None:
                return None
            png: bytes = await self._source.page.screenshot(type="png", full_page=False)
            return png

    # --- internals ---------------------------------------------------------

    async def _connect_ts6(self) -> None:
        log.info(
            "controller.ts6_connecting",
            host=settings.TS6_HOST,
            port=settings.TS6_PORT,
            nickname=settings.TS6_NICKNAME,
            default_channel=settings.TS6_DEFAULT_CHANNEL or "(server default)",
            server_password_set=bool(settings.TS6_SERVER_PASSWORD),
            channel_password_set=bool(settings.TS6_CHANNEL_PASSWORD),
        )
        identity = await load_or_generate_identity(
            settings.IDENTITY_PATH,
            security_level=settings.IDENTITY_SECURITY_LEVEL,
        )
        log.info("controller.ts6_identity_ready", uid=identity.uid)

        client = Ts3Client()
        opts = Ts3ClientOptions(
            host=settings.TS6_HOST,
            port=settings.TS6_PORT,
            identity=identity,
            nickname=settings.TS6_NICKNAME,
            server_password=settings.TS6_SERVER_PASSWORD,
            default_channel=settings.TS6_DEFAULT_CHANNEL,
            channel_password=settings.TS6_CHANNEL_PASSWORD,
        )
        await client.connect(opts)
        self._ts3_client = client
        log.info("controller.ts6_connected", client_id=client.client_id)

    async def _allocate_stream(self) -> None:
        if self._ts3_client is None:
            return

        signaling = StreamSignaling(self._ts3_client)

        capture_config = VideoCaptureConfig(
            display=settings.DISPLAY,
            width=settings.SCREEN_WIDTH,
            height=settings.SCREEN_HEIGHT,
            framerate=settings.SCREEN_FPS,
            pulse_source=f"{settings.PULSE_SINK}.monitor",
        )
        capture = VideoCapture(capture_config)
        publisher = StreamPublisher(client=self._ts3_client, signaling=signaling, capture=capture)

        log.info(
            "controller.stream_setup",
            bitrate=settings.STREAM_BITRATE,
            accessibility=settings.STREAM_ACCESSIBILITY,
            mode=settings.STREAM_MODE,
            viewer_limit=settings.STREAM_VIEWER_LIMIT,
        )
        await publisher.start(
            name=f"{settings.TS6_NICKNAME} Stream",
            bitrate=settings.STREAM_BITRATE,
            accessibility=settings.STREAM_ACCESSIBILITY,
            mode=settings.STREAM_MODE,
            viewer_limit=settings.STREAM_VIEWER_LIMIT,
        )

        self._signaling = signaling
        self._capture = capture
        self._publisher = publisher

    async def _teardown_source_locked(self) -> None:
        if self._source is not None:
            try:
                await self._source.close()
            except Exception as e:
                log.warning("controller.source_close_failed", error=str(e))
            self._source = None
        self._url = None
        self._state = StreamState.IDLE

    def _status_locked(self) -> StreamStatus:
        ts6_connected = self._ts3_client is not None and self._ts3_client.client_id != 0
        publisher_status = self._publisher.status() if self._publisher is not None else None

        return StreamStatus(
            state=self._state,
            url=self._url,
            title=self._source.title() if self._source else None,
            source_class=type(self._source).__name__ if self._source else None,
            error=self._error,
            ts6_connected=ts6_connected,
            ts6_client_id=self._ts3_client.client_id if self._ts3_client else None,
            streaming=publisher_status.streaming if publisher_status else False,
            stream_id=publisher_status.stream_id if publisher_status else None,
            viewer_count=publisher_status.viewer_count if publisher_status else 0,
        )

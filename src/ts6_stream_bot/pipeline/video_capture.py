"""x11grab + PulseAudio capture exposed as aiortc tracks.

Surfaces two ``MediaStreamTrack``s for the stream publisher to attach
to per-viewer ``RTCPeerConnection`` objects:

* Video: aiortc's built-in ``MediaPlayer(format="x11grab")`` over PyAV.
* Audio: a ``ParecAudioTrack`` we wrote ourselves. PyAV's bundled
  ffmpeg ships without libpulse so the analogous ``MediaPlayer(
  format="pulse")`` blows up with ``no container format 'pulse'``.
  Reading PCM from ``parec`` and re-wrapping it as audio frames is
  both portable and avoids depending on a specific PyAV build.

Two separate streams give us a few dozen ms of A/V drift over a
session vs. a single combined ffmpeg invocation; that's acceptable
for watch-together because everyone sees the same drift.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import structlog
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamTrack

from ts6_stream_bot.pipeline.parec_audio_track import ParecAudioTrack

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class VideoCaptureConfig:
    """All knobs in one place. Defaults match Phase 0's screen geometry."""

    display: str = ":99"
    width: int = 1920
    height: int = 1080
    framerate: int = 30
    pulse_source: str = "bot_sink.monitor"


class VideoCapture:
    """X11 + PulseAudio capture, exposed as two aiortc tracks.

    Lifecycle: ``start()`` opens the underlying capture pipelines,
    ``stop()`` tears them down. ``video_track`` and ``audio_track``
    are valid only between those two calls.
    """

    def __init__(self, config: VideoCaptureConfig | None = None) -> None:
        self._config = config or VideoCaptureConfig()
        self._video_player: MediaPlayer | None = None
        self._audio_track: ParecAudioTrack | None = None

    @property
    def is_running(self) -> bool:
        return self._video_player is not None or self._audio_track is not None

    @property
    def video_track(self) -> MediaStreamTrack | None:
        return self._video_player.video if self._video_player is not None else None

    @property
    def audio_track(self) -> MediaStreamTrack | None:
        return self._audio_track

    async def start(self) -> None:
        """Open the X11 + Pulse capture pipelines. Idempotent."""
        if self.is_running:
            return

        c = self._config
        # x11grab: capture the virtual display at the configured rate.
        self._video_player = MediaPlayer(
            c.display,
            format="x11grab",
            options={
                "video_size": f"{c.width}x{c.height}",
                "framerate": str(c.framerate),
                # Software-only path - matches the GPU-disabled Chromium config.
                "draw_mouse": "1",
            },
        )

        # Pulse via parec - PyAV doesn't ship libpulse so we can't use
        # MediaPlayer(format="pulse") here.
        self._audio_track = ParecAudioTrack(source=c.pulse_source)

        log.info(
            "video_capture.started",
            display=c.display,
            size=f"{c.width}x{c.height}",
            fps=c.framerate,
            pulse=c.pulse_source,
        )

    async def stop(self) -> None:
        """Tear down both players. ``stop()`` is idempotent."""
        if self._video_player is not None:
            for track in (self._video_player.audio, self._video_player.video):
                if track is None:
                    continue
                with contextlib.suppress(Exception):
                    track.stop()
            self._video_player = None

        if self._audio_track is not None:
            with contextlib.suppress(Exception):
                self._audio_track.stop()
            self._audio_track = None

        log.info("video_capture.stopped")


__all__ = [
    "VideoCapture",
    "VideoCaptureConfig",
]

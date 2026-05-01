"""x11grab + PulseAudio capture exposed as aiortc tracks.

Owns two ``MediaPlayer`` instances - one for the X11 framebuffer, one
for the PulseAudio monitor sink - and surfaces their tracks for the
stream publisher to attach to per-viewer ``RTCPeerConnection`` objects.

Why two players: aiortc's ``MediaPlayer`` opens one container at a
time, but TS6 streams want one Opus + one VP8 track in the same SDP.
A single combined ``ffmpeg -f x11grab ... -f pulse ...`` invocation
would give us tight A/V sync, but at the cost of a custom RTP
demuxer to feed aiortc. Two players is a few dozen ms of drift over
a session - acceptable for watch-together where everyone sees the
same drift.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import structlog
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamTrack

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

    Lifecycle: ``start()`` opens the underlying ffmpeg subprocesses,
    ``stop()`` closes them. ``video_track`` and ``audio_track`` are
    valid only between those two calls.
    """

    def __init__(self, config: VideoCaptureConfig | None = None) -> None:
        self._config = config or VideoCaptureConfig()
        self._video_player: MediaPlayer | None = None
        self._audio_player: MediaPlayer | None = None

    @property
    def is_running(self) -> bool:
        return self._video_player is not None or self._audio_player is not None

    @property
    def video_track(self) -> MediaStreamTrack | None:
        return self._video_player.video if self._video_player is not None else None

    @property
    def audio_track(self) -> MediaStreamTrack | None:
        return self._audio_player.audio if self._audio_player is not None else None

    async def start(self) -> None:
        """Open the X11 + Pulse ffmpeg pipelines. Idempotent."""
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

        # Pulse: read from the monitor source so we capture whatever
        # Chromium plays into bot_sink.
        self._audio_player = MediaPlayer(
            c.pulse_source,
            format="pulse",
            options={
                "sample_rate": "48000",
                "channels": "2",
            },
        )

        log.info(
            "video_capture.started",
            display=c.display,
            size=f"{c.width}x{c.height}",
            fps=c.framerate,
            pulse=c.pulse_source,
        )

    async def stop(self) -> None:
        """Tear down both players. ``stop()`` is idempotent."""
        # MediaPlayer.audio / .video return MediaStreamTrack objects that
        # internally reference the player; calling .stop() on them shuts
        # down the underlying ffmpeg.
        for player in (self._video_player, self._audio_player):
            if player is None:
                continue
            for track in (player.audio, player.video):
                if track is None:
                    continue
                with contextlib.suppress(Exception):
                    track.stop()

        self._video_player = None
        self._audio_player = None
        log.info("video_capture.stopped")


__all__ = [
    "VideoCapture",
    "VideoCaptureConfig",
]

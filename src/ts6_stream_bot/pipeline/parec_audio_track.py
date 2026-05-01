"""aiortc audio track backed by ``parec``.

PyAV's bundled ffmpeg ships without libpulse, so
``MediaPlayer(format="pulse", ...)`` blows up with
``no container format 'pulse'``. Instead of fighting that, we read raw
PCM directly from ``parec`` (PulseAudio's standard CLI capture tool)
and wrap it in our own ``MediaStreamTrack`` so aiortc can fan it out
to per-viewer peer connections like any other track.

PCM frame size: 20 ms of stereo s16-le @ 48 kHz = 960 samples per
channel = 3840 bytes - same as the ``audio_capture`` module that feeds
the TS3 voice channel. PulseAudio paces ``parec`` at real time, so the
track's ``recv()`` simply blocks on the next chunk.
"""

from __future__ import annotations

import asyncio
import contextlib
from fractions import Fraction

import av
import numpy as np
import structlog
from aiortc.mediastreams import AUDIO_PTIME, MediaStreamError, MediaStreamTrack

log = structlog.get_logger(__name__)

_SAMPLE_RATE = 48000
_CHANNELS = 2
_SAMPLES_PER_FRAME = int(_SAMPLE_RATE * AUDIO_PTIME)  # 960 @ 20 ms
_PCM_BYTES_PER_FRAME = _SAMPLES_PER_FRAME * _CHANNELS * 2


class ParecAudioTrack(MediaStreamTrack):
    """Streams 48 kHz stereo s16-le PCM from a PulseAudio source as
    aiortc audio frames. The first ``recv()`` call lazily spawns parec.
    """

    kind = "audio"

    def __init__(
        self,
        *,
        source: str = "bot_sink.monitor",
        capture_argv: list[str] | None = None,
    ) -> None:
        """``capture_argv`` overrides the parec invocation for tests."""
        super().__init__()
        self._source = source
        self._capture_argv = capture_argv or [
            "parec",
            f"--device={source}",
            "--format=s16le",
            f"--rate={_SAMPLE_RATE}",
            f"--channels={_CHANNELS}",
            "--raw",
        ]
        self._proc: asyncio.subprocess.Process | None = None
        self._start_lock = asyncio.Lock()
        self._pts = 0
        self._stopped = False

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._proc is not None or self._stopped:
                return
            self._proc = await asyncio.create_subprocess_exec(
                *self._capture_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("parec_audio.started", source=self._source, pid=self._proc.pid)

    async def recv(self) -> av.AudioFrame:
        if self._stopped:
            raise MediaStreamError("track is stopped")

        await self._ensure_started()
        assert self._proc is not None and self._proc.stdout is not None

        try:
            chunk = await self._proc.stdout.readexactly(_PCM_BYTES_PER_FRAME)
        except asyncio.IncompleteReadError as exc:
            log.warning("parec_audio.eof", got=len(exc.partial))
            self._stopped = True
            raise MediaStreamError("parec ended unexpectedly") from exc

        pcm = np.frombuffer(chunk, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
        frame.sample_rate = _SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, _SAMPLE_RATE)
        self._pts += _SAMPLES_PER_FRAME
        return frame

    def stop(self) -> None:
        super().stop()
        self._stopped = True
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            self._proc = None
            log.info("parec_audio.stopped")


__all__ = ["ParecAudioTrack"]

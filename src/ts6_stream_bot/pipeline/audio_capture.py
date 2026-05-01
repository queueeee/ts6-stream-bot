"""PulseAudio capture -> Opus encode -> per-frame callback.

The bot's PulseAudio default sink is a virtual ``bot_sink`` (configured
in ``docker/pulse/default.pa``); Chromium plays audio there, and a
``bot_sink.monitor`` source lets us tap that output. We spawn ``parec``
to read raw 48 kHz s16-le stereo from the monitor at real-time pacing
(PulseAudio already paces it for us), encode each 20 ms slice with
libopus via PyAV, and hand the resulting Opus packet to a callback -
typically ``Ts3Client.send_voice``.

20 ms of stereo s16-le @ 48 kHz = 960 samples per channel = 3840 bytes
on the wire. That's the TS3 voice protocol's expected frame size.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import av
import numpy as np
import structlog

log = structlog.get_logger(__name__)

OPUS_SAMPLE_RATE = 48000
OPUS_CHANNELS = 2
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME = OPUS_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 960
PCM_BYTES_PER_FRAME = SAMPLES_PER_FRAME * OPUS_CHANNELS * 2  # 3840


class AudioCapture:
    """Run parec + an Opus encoder, push Opus frames to a callback.

    Lifetime is one ``await start()`` / ``await stop()`` pair. Re-using
    the same instance after a stop is supported.
    """

    def __init__(
        self,
        *,
        sink_monitor: str,
        on_opus_frame: Callable[[bytes], None],
        opus_bit_rate: int = 128000,
        capture_argv: list[str] | None = None,
    ) -> None:
        """``capture_argv`` overrides the default ``parec`` invocation; tests
        use it to stand in a deterministic PCM-producing subprocess."""
        self._sink_monitor = sink_monitor
        self._on_opus_frame = on_opus_frame
        self._opus_bit_rate = opus_bit_rate
        self._capture_argv = capture_argv or [
            "parec",
            f"--device={sink_monitor}",
            "--format=s16le",
            f"--rate={OPUS_SAMPLE_RATE}",
            f"--channels={OPUS_CHANNELS}",
            "--raw",
        ]

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._encoder: av.codec.context.CodecContext | None = None

    @property
    def is_running(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def start(self) -> None:
        """Spawn parec and start encoding. Returns immediately; the loop
        runs as a background task. Call ``stop()`` to tear down."""
        if self.is_running:
            return

        self._encoder = self._build_encoder()
        self._proc = await asyncio.create_subprocess_exec(
            *self._capture_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        log.info(
            "audio.capture_started",
            sink=self._sink_monitor,
            pid=self._proc.pid,
            bit_rate=self._opus_bit_rate,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        """Stop parec, flush the encoder, cancel background tasks."""
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()
            self._proc = None

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

        # Flush the encoder so any tail packet still gets sent.
        if self._encoder is not None:
            for pkt in self._encoder.encode(None):
                self._dispatch_packet(pkt)
            self._encoder = None

        log.info("audio.capture_stopped")

    # --- internals -------------------------------------------------------

    def _build_encoder(self) -> av.codec.context.CodecContext:
        ctx = av.codec.CodecContext.create("libopus", "w")
        ctx.sample_rate = OPUS_SAMPLE_RATE
        ctx.layout = "stereo"
        ctx.format = "s16"
        ctx.bit_rate = self._opus_bit_rate
        return ctx

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        assert self._encoder is not None
        try:
            while True:
                chunk = await self._proc.stdout.readexactly(PCM_BYTES_PER_FRAME)
                self._encode_and_dispatch(chunk)
        except asyncio.IncompleteReadError as exc:
            # parec closed - normal on shutdown, log only if we got a
            # truncated tail without explicit stop().
            if exc.partial:
                log.debug("audio.parec_truncated_tail", bytes=len(exc.partial))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("audio.read_loop_failed", error=str(exc))

    def _encode_and_dispatch(self, pcm_bytes: bytes) -> None:
        assert self._encoder is not None
        # ndarray shape for packed (interleaved) s16 stereo is (1, samples*channels).
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
        frame.sample_rate = OPUS_SAMPLE_RATE
        for pkt in self._encoder.encode(frame):
            self._dispatch_packet(pkt)

    def _dispatch_packet(self, packet: av.Packet) -> None:
        try:
            self._on_opus_frame(bytes(packet))
        except Exception as exc:
            log.exception("audio.callback_failed", error=str(exc))

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                log.debug("audio.parec_stderr", line=line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise


__all__ = [
    "FRAME_DURATION_MS",
    "OPUS_CHANNELS",
    "OPUS_SAMPLE_RATE",
    "PCM_BYTES_PER_FRAME",
    "SAMPLES_PER_FRAME",
    "AudioCapture",
]

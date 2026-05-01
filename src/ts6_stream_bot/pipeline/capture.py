"""ffmpeg subprocess that captures the X11 display + PulseAudio monitor and
encodes to HLS.

The ffmpeg invocation is intentionally simple. If you want to tune parameters,
go through `Settings`, not this file.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import structlog

from ts6_stream_bot.config import settings

log = structlog.get_logger(__name__)


class HlsCapture:
    """Spawns and supervises an ffmpeg process that produces HLS segments."""

    def __init__(self, room: str) -> None:
        self.room = room
        self.output_dir = settings.HLS_OUTPUT_DIR / room
        self.playlist_path = self.output_dir / "index.m3u8"
        self._proc: asyncio.subprocess.Process | None = None

    def _build_args(self) -> list[str]:
        keyframe_interval = settings.SCREEN_FPS * settings.HLS_SEGMENT_DURATION
        args: list[str] = [
            "ffmpeg",
            "-loglevel", "warning",
            "-nostdin",

            # Video input: x11grab
            "-f", "x11grab",
            "-video_size", f"{settings.SCREEN_WIDTH}x{settings.SCREEN_HEIGHT}",
            "-framerate", str(settings.SCREEN_FPS),
            "-draw_mouse", "0",
            "-i", settings.DISPLAY,

            # Audio input: PulseAudio monitor of bot_sink
            "-f", "pulse",
            "-i", f"{settings.PULSE_SINK}.monitor",
        ]

        # Optional loudnorm filter to normalise levels across sources.
        # Single-pass mode is approximate but introduces no extra latency.
        if settings.AUDIO_LOUDNORM:
            args += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]

        args += [
            # Video encode
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", str(keyframe_interval),
            "-keyint_min", str(keyframe_interval),
            "-sc_threshold", "0",

            # Audio encode
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",

            # HLS output
            "-f", "hls",
            "-hls_time", str(settings.HLS_SEGMENT_DURATION),
            "-hls_list_size", str(settings.HLS_PLAYLIST_SIZE),
            "-hls_flags", "delete_segments+independent_segments+omit_endlist",
            "-hls_segment_filename", str(self.output_dir / "seg_%05d.ts"),
            str(self.playlist_path),
        ]
        return args

    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        # Wipe old output
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir, ignore_errors=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        args = self._build_args()
        log.info("capture.starting", room=self.room, output=str(self.playlist_path))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Drain stderr so it shows up in our logs
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="replace").rstrip()
                if msg:
                    log.info("ffmpeg", msg=msg)
        except Exception as e:
            log.warning("capture.stderr_drain_failed", error=str(e))

    async def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.returncode is None:
            log.info("capture.stopping")
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    log.warning("capture.kill_after_timeout")
                    self._proc.kill()
                    await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def stream_url_path(self) -> str:
        """The relative URL path nginx serves under."""
        return f"/stream/{self.room}/index.m3u8"

"""ffmpeg subprocess that captures the X11 display + PulseAudio monitor and
encodes to HLS.

The ffmpeg invocation is intentionally simple. If you want to tune parameters,
go through `Settings`, not this file.
"""

from __future__ import annotations

import asyncio
import shutil

import structlog

from ts6_stream_bot.config import settings
from ts6_stream_bot.utils import paths
from ts6_stream_bot.utils.proc import drain_stream_to_log, graceful_terminate

log = structlog.get_logger(__name__)


class HlsCapture:
    """Spawns and supervises an ffmpeg process that produces HLS segments."""

    def __init__(self, room: str) -> None:
        self.room = paths.validate_room(room)
        self.output_dir = paths.hls_dir(self.room)
        self.playlist_path = paths.hls_playlist(self.room)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None

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
            "-hls_segment_filename", paths.hls_segment_pattern(self.room),
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

        # Drain stderr into our logger; keep a reference so it isn't GC'd.
        self._stderr_task = asyncio.create_task(
            drain_stream_to_log(
                self._proc.stderr,
                log_event="ffmpeg",
                extra_kwargs={"room": self.room},
            )
        )

    async def stop(self) -> None:
        if self._proc is None:
            return
        log.info("capture.stopping", room=self.room)
        await graceful_terminate(self._proc, timeout=5, name="ffmpeg")
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        self._proc = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def stream_url_path(self) -> str:
        """The relative URL path nginx serves under."""
        return paths.stream_url_path(self.room)

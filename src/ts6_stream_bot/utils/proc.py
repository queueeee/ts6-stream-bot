"""Subprocess helpers with graceful shutdown.

ffmpeg, pactl, etc. all share the same lifecycle pattern: spawn, drain stderr
into structlog, terminate cleanly with SIGKILL fallback. Centralizing it here
keeps `pipeline/capture.py` focused on the ffmpeg argv.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog

log = structlog.get_logger(__name__)


async def graceful_terminate(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float = 5.0,
    name: str = "subprocess",
) -> None:
    """Send SIGTERM, wait `timeout` seconds, then SIGKILL if still alive."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            log.warning("proc.kill_after_timeout", name=name, timeout=timeout)
            proc.kill()
            await proc.wait()
    except ProcessLookupError:
        # Already gone between our check and the signal.
        pass


async def drain_stream_to_log(
    stream: asyncio.StreamReader | None,
    *,
    log_event: str,
    extra_kwargs: dict[str, str] | None = None,
) -> None:
    """Read lines from `stream` until EOF and emit each as a structlog event."""
    if stream is None:
        return
    extras = extra_kwargs or {}
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            msg = line.decode(errors="replace").rstrip()
            if msg:
                log.info(log_event, msg=msg, **extras)
    except Exception as e:  # broken pipe, cancellation, etc. - log and exit
        log.warning("proc.drain_failed", error=str(e), **extras)


async def run_capture(args: Sequence[str]) -> tuple[bytes, bytes, int]:
    """Run a short-lived command and return (stdout, stderr, returncode).

    Use this for one-shot CLI tools (pactl, ffprobe, etc.). Do NOT use it for
    long-running processes - those should manage their own lifecycle.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode or 0

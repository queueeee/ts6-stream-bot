"""Tests for utils.proc - graceful_terminate + drain_stream_to_log."""

from __future__ import annotations

import asyncio

import pytest

from ts6_stream_bot.utils.proc import (
    drain_stream_to_log,
    graceful_terminate,
    run_capture,
)


@pytest.mark.asyncio
async def test_run_capture_returns_stdout_and_returncode() -> None:
    stdout, _, rc = await run_capture(["sh", "-c", "echo hello && exit 0"])
    assert rc == 0
    assert b"hello" in stdout


@pytest.mark.asyncio
async def test_run_capture_propagates_returncode() -> None:
    _, _, rc = await run_capture(["sh", "-c", "exit 7"])
    assert rc == 7


@pytest.mark.asyncio
async def test_graceful_terminate_sigterm_path() -> None:
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", "sleep 30",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await graceful_terminate(proc, timeout=2, name="sleeper")
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_graceful_terminate_sigkill_fallback() -> None:
    # Python child that explicitly ignores SIGTERM, forcing the SIGKILL fallback.
    # We wait on a "ready" line so we don't race the interpreter startup vs the SIGTERM.
    proc = await asyncio.create_subprocess_exec(
        "python3", "-u", "-c",
        "import signal, sys, time;"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        " sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
    assert line.strip() == b"ready"
    await graceful_terminate(proc, timeout=0.5, name="stubborn")
    assert proc.returncode is not None
    # SIGKILL exits with -9 (negative signal) on POSIX.
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_graceful_terminate_already_dead_is_noop() -> None:
    proc = await asyncio.create_subprocess_exec(
        "sh", "-c", "exit 0",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    # Should not raise even though the process is already gone.
    await graceful_terminate(proc, timeout=1, name="dead")


@pytest.mark.asyncio
async def test_drain_stream_to_log_handles_none() -> None:
    # Just exercise the early-return path; nothing to assert beyond no-raise.
    await drain_stream_to_log(None, log_event="x")

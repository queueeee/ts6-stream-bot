"""PulseAudio sink/monitor wiring helpers.

The Docker entrypoint loads the virtual `bot_sink` from
`docker/pulse/default.pa` at boot. This module is for runtime introspection
(used by the debug API) and idempotent re-creation when running outside
Docker (the dev script).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ts6_stream_bot.config import settings
from ts6_stream_bot.utils.proc import run_capture

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class PulseSink:
    index: int
    name: str
    driver: str
    sample_spec: str
    state: str


async def list_sinks() -> list[PulseSink]:
    """Return the currently loaded PulseAudio sinks. Empty list on any error."""
    stdout, stderr, rc = await run_capture(["pactl", "list", "sinks", "short"])
    if rc != 0:
        log.warning("audio.list_sinks_failed", rc=rc, stderr=stderr.decode(errors="replace"))
        return []
    sinks: list[PulseSink] = []
    for line in stdout.decode(errors="replace").splitlines():
        # Tab-separated: index name driver sample-spec state
        parts = line.split("\t")
        if len(parts) >= 5:
            try:
                sinks.append(
                    PulseSink(
                        index=int(parts[0]),
                        name=parts[1],
                        driver=parts[2],
                        sample_spec=parts[3],
                        state=parts[4],
                    )
                )
            except ValueError:
                continue
    return sinks


async def sink_exists(name: str) -> bool:
    return any(s.name == name for s in await list_sinks())


async def ensure_sink(name: str | None = None) -> bool:
    """Idempotently create the configured null-sink. Returns True if it now exists."""
    target = name or settings.PULSE_SINK
    if await sink_exists(target):
        return True
    _, stderr, rc = await run_capture(
        ["pactl", "load-module", "module-null-sink", f"sink_name={target}"]
    )
    if rc != 0:
        log.warning(
            "audio.ensure_sink_failed",
            sink=target,
            rc=rc,
            stderr=stderr.decode(errors="replace"),
        )
        return False
    log.info("audio.sink_created", sink=target)
    return await sink_exists(target)


async def get_default_sink() -> str | None:
    stdout, _, rc = await run_capture(["pactl", "get-default-sink"])
    if rc != 0:
        return None
    return stdout.decode(errors="replace").strip() or None

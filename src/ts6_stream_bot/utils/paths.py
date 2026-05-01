"""Filesystem and URL path helpers for HLS output.

Single source of truth for where capture writes its segments and what URL nginx
serves them under. Anything that touches HLS paths should go through here so
changes don't have to ripple across capture / controller / nginx config.
"""

from __future__ import annotations

import re
from pathlib import Path

from ts6_stream_bot.config import settings

# Room names appear in filesystem paths and URLs; keep them tame.
_VALID_ROOM = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_room(room: str) -> str:
    """Reject path-injection attempts and other unfriendly characters."""
    if not _VALID_ROOM.fullmatch(room):
        raise ValueError(
            f"invalid room name: {room!r} (allowed: A-Z a-z 0-9 _ -, max 64 chars)"
        )
    return room


def hls_dir(room: str) -> Path:
    """Filesystem directory where segments + playlist for `room` live."""
    return settings.HLS_OUTPUT_DIR / validate_room(room)


def hls_playlist(room: str) -> Path:
    """Path to the rolling HLS playlist file for `room`."""
    return hls_dir(room) / "index.m3u8"


def hls_segment_pattern(room: str) -> str:
    """ffmpeg `-hls_segment_filename` pattern for `room`."""
    return str(hls_dir(room) / "seg_%05d.ts")


def stream_url_path(room: str) -> str:
    """Public URL path nginx serves the playlist under."""
    return f"/stream/{validate_room(room)}/index.m3u8"

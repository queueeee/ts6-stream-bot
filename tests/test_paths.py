"""Path helpers + room name validation."""

from __future__ import annotations

import pytest

from ts6_stream_bot.config import settings
from ts6_stream_bot.utils import paths


def test_validate_room_accepts_sane_names() -> None:
    for name in ("default", "room1", "room_2", "abc-123", "A_b-C"):
        assert paths.validate_room(name) == name


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../etc",
        "room/with/slash",
        "room with spaces",
        "room.dot",
        "weird;chars",
        "x" * 65,
    ],
)
def test_validate_room_rejects_bad_names(bad: str) -> None:
    with pytest.raises(ValueError):
        paths.validate_room(bad)


def test_hls_paths_compose_under_output_dir() -> None:
    room = "default"
    assert paths.hls_dir(room) == settings.HLS_OUTPUT_DIR / room
    assert paths.hls_playlist(room) == settings.HLS_OUTPUT_DIR / room / "index.m3u8"
    assert paths.hls_segment_pattern(room).endswith("/seg_%05d.ts")
    assert paths.stream_url_path(room) == "/stream/default/index.m3u8"


def test_path_helpers_validate_room() -> None:
    with pytest.raises(ValueError):
        paths.hls_dir("../etc")
    with pytest.raises(ValueError):
        paths.stream_url_path("../etc")

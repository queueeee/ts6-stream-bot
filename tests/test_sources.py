"""Unit tests for source resolution."""

from __future__ import annotations

import pytest

from ts6_stream_bot.sources import SOURCES, _discover_operator_sources, resolve_source
from ts6_stream_bot.sources.browser_url import BrowserUrlSource
from ts6_stream_bot.sources.direct_file import DirectFileSource
from ts6_stream_bot.sources.twitch import TwitchSource
from ts6_stream_bot.sources.youtube import YoutubeSource


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", YoutubeSource),
        ("https://youtu.be/dQw4w9WgXcQ", YoutubeSource),
        ("https://m.youtube.com/watch?v=abc123", YoutubeSource),
        ("https://www.twitch.tv/some_streamer", TwitchSource),
        ("https://twitch.tv/some_streamer", TwitchSource),
        ("https://m.twitch.tv/some_streamer", TwitchSource),
        ("https://clips.twitch.tv/SomeClipSlug", TwitchSource),
        ("https://example.com/video.mp4", DirectFileSource),
        ("https://example.com/stream.m3u8", DirectFileSource),
        ("file:///srv/movies/foo.mkv", DirectFileSource),
        ("https://vimeo.com/12345", BrowserUrlSource),
        ("https://example.com/", BrowserUrlSource),
    ],
)
def test_resolve_source(url: str, expected: type) -> None:
    assert resolve_source(url) is expected


def test_browser_url_source_is_last() -> None:
    """BrowserUrlSource MUST be last in registry - it's the catch-all fallback."""
    assert SOURCES[-1] is BrowserUrlSource


def test_browser_url_source_accepts_anything_http() -> None:
    assert BrowserUrlSource.can_handle("https://anything.example/")
    assert BrowserUrlSource.can_handle("http://10.0.0.1/path")


def test_twitch_does_not_match_unrelated_domains() -> None:
    assert not TwitchSource.can_handle("https://nottwitch.tv/foo")
    assert not TwitchSource.can_handle("https://example.com/twitch.tv")


def test_template_is_not_auto_registered() -> None:
    """The skeleton in _operator_implemented/_template.py must NEVER be picked
    up by the discovery hook, regardless of whether the operator has dropped
    real sources alongside it."""
    for cls in SOURCES:
        assert not cls.__name__.startswith("_OperatorTemplate"), (
            f"_template.py was auto-registered: {cls!r}"
        )


def test_no_operator_sources_by_default() -> None:
    """With nothing in _operator_implemented/ besides the README/template,
    discovery returns an empty list and SOURCES is the built-in lineup."""
    assert _discover_operator_sources() == []


def test_discovery_picks_up_a_user_module() -> None:
    """Drop a synthetic source into _operator_implemented/, run discovery,
    confirm it shows up. Cleans up after itself."""
    from pathlib import Path

    pkg_dir = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "ts6_stream_bot"
        / "sources"
        / "_operator_implemented"
    )
    test_file = pkg_dir / "test_synthetic.py"
    test_file.write_text(
        "from ts6_stream_bot.sources.base import StreamSource\n"
        "class SyntheticSource(StreamSource):\n"
        "    @classmethod\n"
        "    def can_handle(cls, url): return False\n"
        "    async def open(self, context, url): return None\n"
        "    async def play(self): return None\n"
        "    async def pause(self): return None\n"
        "    async def seek(self, seconds): return None\n"
        "    async def close(self): return None\n",
        encoding="utf-8",
    )
    try:
        found = _discover_operator_sources()
        names = [c.__name__ for c in found]
        assert "SyntheticSource" in names
    finally:
        test_file.unlink(missing_ok=True)

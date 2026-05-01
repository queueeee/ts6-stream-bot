"""Unit tests for source resolution."""

from __future__ import annotations

import pytest

from ts6_stream_bot.sources import SOURCES, resolve_source
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

"""Source registry. URLs are dispatched to the first source whose can_handle() returns True."""

from __future__ import annotations

from ts6_stream_bot.sources.base import StreamSource
from ts6_stream_bot.sources.browser_url import BrowserUrlSource
from ts6_stream_bot.sources.direct_file import DirectFileSource
from ts6_stream_bot.sources.youtube import YoutubeSource

# Order matters: more specific sources come first. BrowserUrlSource MUST be last
# because it accepts everything as a fallback.
SOURCES: list[type[StreamSource]] = [
    YoutubeSource,
    DirectFileSource,
    BrowserUrlSource,
]


def resolve_source(url: str) -> type[StreamSource]:
    """Find the first source class that claims to handle this URL."""
    for src in SOURCES:
        if src.can_handle(url):
            return src
    # Should be unreachable because BrowserUrlSource accepts everything,
    # but guard anyway.
    raise ValueError(f"no source can handle url: {url}")


__all__ = ["SOURCES", "StreamSource", "resolve_source"]

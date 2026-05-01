"""Source registry. URLs are dispatched to the first source whose can_handle() returns True."""

from __future__ import annotations

import importlib
from pathlib import Path

import structlog

from ts6_stream_bot.sources.base import StreamSource
from ts6_stream_bot.sources.browser_url import BrowserUrlSource
from ts6_stream_bot.sources.direct_file import DirectFileSource
from ts6_stream_bot.sources.twitch import TwitchSource
from ts6_stream_bot.sources.youtube import YoutubeSource

log = structlog.get_logger(__name__)


def _discover_operator_sources() -> list[type[StreamSource]]:
    """Auto-import any *.py file the operator dropped into _operator_implemented/.

    Files whose name starts with `_` are skipped (so __init__.py and the
    _template.py example don't get picked up). Each module is searched for
    StreamSource subclasses defined in that module; matches are returned in
    file-name order.

    The directory itself is gitignored except for the template, README and
    __init__.py — see CLAUDE.md "Operator-Implemented Parts".
    """
    pkg_dir = Path(__file__).parent / "_operator_implemented"
    if not pkg_dir.is_dir():
        return []
    found: list[type[StreamSource]] = []
    for path in sorted(pkg_dir.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        mod_name = f"ts6_stream_bot.sources._operator_implemented.{path.stem}"
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            log.warning(
                "operator_source.import_failed",
                module=mod_name,
                error=str(exc),
            )
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, StreamSource)
                and attr is not StreamSource
                and attr.__module__ == mod_name
            ):
                found.append(attr)
                log.info(
                    "operator_source.registered",
                    source=attr.__name__,
                    module=mod_name,
                )
    return found


# Order matters: more specific sources come first. Operator-supplied sources
# are inserted just before BrowserUrlSource so they get a chance to claim
# their URLs before the catch-all fallback. BrowserUrlSource MUST stay last.
SOURCES: list[type[StreamSource]] = [
    YoutubeSource,
    TwitchSource,
    DirectFileSource,
    *_discover_operator_sources(),
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

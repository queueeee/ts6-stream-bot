"""Abstract base class for all stream sources.

A "source" is anything that can be opened in the controlled Chromium instance
to produce video+audio output, plus offers playback control (play/pause/seek/close).

To add a new source:
  1. Subclass StreamSource in a new file under sources/
  2. Implement all abstract methods
  3. Register the class in sources/__init__.py SOURCES list, BEFORE BrowserUrlSource

Anything DRM-related is OUT OF SCOPE for this codebase. See CLAUDE.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page


class StreamSource(ABC):
    """A pluggable source that renders into a Chromium page.

    Lifecycle:
        can_handle(url)   -> bool                  (classmethod, checks URL)
        __init__(...)     -> instance
        await open(ctx, url)                       (navigates, prepares playback)
        await play()      -> starts playback
        await pause()     -> pauses
        await seek(s)     -> seeks to absolute second
        title()           -> human-readable title or None
        await close()     -> teardown
    """

    def __init__(self) -> None:
        self._page: Page | None = None
        self._title: str | None = None

    # --- URL routing -------------------------------------------------------

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Return True if this source can handle the given URL.

        Called by the source registry during `POST /play` resolution.
        First match wins (registry order).
        """

    # --- Lifecycle ---------------------------------------------------------

    @abstractmethod
    async def open(self, context: BrowserContext, url: str) -> None:
        """Navigate Chromium to the URL and prepare playback.

        Should NOT auto-play - the controller calls play() afterwards.
        Should set self._page and self._title.
        """

    @abstractmethod
    async def play(self) -> None:
        """Start (or resume) playback."""

    @abstractmethod
    async def pause(self) -> None:
        """Pause playback."""

    @abstractmethod
    async def seek(self, seconds: int) -> None:
        """Seek to absolute time in seconds from start."""

    @abstractmethod
    async def close(self) -> None:
        """Stop playback and clean up the page."""

    # --- Introspection -----------------------------------------------------

    def title(self) -> str | None:
        """Human-readable title for the currently loaded media."""
        return self._title

    @property
    def page(self) -> Page | None:
        return self._page

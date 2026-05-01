"""Template for an operator-implemented StreamSource.

Copy this file (e.g. `cp _template.py myservice.py`), rename the class,
fill in the `TODO(operator)` markers, then restart the bot. The discovery
hook in `sources/__init__.py` will auto-register the new class.

This template intentionally contains NO logic for circumventing DRM,
extracting Widevine CDMs, or decrypting protected streams. That work is
out of scope for the upstream codebase and for any AI assistant working
on it. If you choose to implement protected sources locally, the legal
and operational responsibility is entirely yours.

The discovery hook skips files whose name starts with `_`, so this
template itself is never instantiated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ts6_stream_bot.sources.base import StreamSource

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = structlog.get_logger(__name__)


class _OperatorTemplate(StreamSource):
    """Rename me to something descriptive (e.g. `MyServiceSource`)."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        # TODO(operator): return True for URLs this source should handle.
        # First-match-wins routing; keep the predicate tight so other
        # sources still see the URLs they own.
        return False

    async def open(self, context: BrowserContext, url: str) -> None:
        # TODO(operator): open a new page, navigate to `url`, prepare
        # playback (cookie banners, login flow, ad skipping, …), then set
        # `self._page = page` and optionally `self._title`.
        #
        # Do NOT autoplay - the controller calls `play()` afterwards.
        raise NotImplementedError("operator must implement open()")

    async def play(self) -> None:
        # TODO(operator): start (or resume) playback, typically via
        # `self._page.evaluate("document.querySelector('video')?.play()")`.
        raise NotImplementedError("operator must implement play()")

    async def pause(self) -> None:
        # TODO(operator): pause playback.
        raise NotImplementedError("operator must implement pause()")

    async def seek(self, seconds: int) -> None:
        # TODO(operator): seek to absolute second.
        raise NotImplementedError("operator must implement seek()")

    async def close(self) -> None:
        # TODO(operator): close the page and release any per-source state.
        raise NotImplementedError("operator must implement close()")

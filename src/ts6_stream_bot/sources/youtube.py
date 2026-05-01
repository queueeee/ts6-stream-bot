"""YouTube source.

Loads a video in the standard YouTube watch UI and drives play/pause/seek via
the IFrame Player API exposed on the page (window.movie_player or the HTML5 video).
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING

import structlog

from ts6_stream_bot.sources.base import StreamSource

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = structlog.get_logger(__name__)

_YOUTUBE_HOST_PATTERN = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/)",
    re.IGNORECASE,
)


class YoutubeSource(StreamSource):
    """Plays a YouTube video by driving the page's HTML5 video element."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_YOUTUBE_HOST_PATTERN.match(url))

    async def open(self, context: BrowserContext, url: str) -> None:
        log.info("youtube.open", url=url)
        page = await context.new_page()
        self._page = page
        await page.goto(url, wait_until="domcontentloaded")

        # Dismiss the cookie consent banner if present (EU). Banner is optional.
        with suppress(Exception):
            await page.locator('button:has-text("Accept all")').first.click(timeout=3000)

        # Wait for the video element to exist
        await page.wait_for_selector("video", timeout=15000)

        # Force the player to NOT autoplay; we trigger play() ourselves
        await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (v) { v.pause(); v.currentTime = 0; }
            }
        """)

        # Try to grab the title
        try:
            title = await page.title()
            self._title = title.replace(" - YouTube", "").strip() or None
        except Exception:
            self._title = None

    async def play(self) -> None:
        if self._page is None:
            return
        await self._page.evaluate("document.querySelector('video')?.play()")

    async def pause(self) -> None:
        if self._page is None:
            return
        await self._page.evaluate("document.querySelector('video')?.pause()")

    async def seek(self, seconds: int) -> None:
        if self._page is None:
            return
        await self._page.evaluate(
            "(s) => { const v = document.querySelector('video'); if (v) v.currentTime = s; }",
            seconds,
        )

    async def close(self) -> None:
        if self._page is not None:
            with suppress(Exception):
                await self._page.close()
            self._page = None

"""Generic browser source: navigates Chromium to any URL and assumes the page
has a single <video> element that can be controlled via the standard HTML5 API.

This is the catch-all fallback. Always last in the SOURCES registry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ts6_stream_bot.sources.base import StreamSource

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = structlog.get_logger(__name__)


class BrowserUrlSource(StreamSource):
    """Open any URL and best-effort drive the first <video> element on the page."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        # Last-resort fallback - accepts anything that looks like a URL
        return url.startswith(("http://", "https://", "file://"))

    async def open(self, context: BrowserContext, url: str) -> None:
        log.info("browser_url.open", url=url)
        page = await context.new_page()
        self._page = page
        await page.goto(url, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector("video", timeout=10000)
        except Exception:
            log.warning("browser_url.no_video_element", url=url)
            # Source still works as "screen-share-anything"; just no video controls

        try:
            self._title = await page.title()
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
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

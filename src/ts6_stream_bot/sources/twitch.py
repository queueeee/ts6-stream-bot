"""Twitch source.

Handles two URL shapes:
  * Live channels: https://www.twitch.tv/<channel>
  * Clips:         https://clips.twitch.tv/<id>  or  https://www.twitch.tv/<channel>/clip/<id>

Twitch streams its content as HLS to the page's <video> element, so once the
page is loaded we drive playback the same way as the other browser-based sources.
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

_TWITCH_HOST_PATTERN = re.compile(
    r"^https?://(?:www\.|m\.)?(?:twitch\.tv|clips\.twitch\.tv)(?:/|$)",
    re.IGNORECASE,
)


class TwitchSource(StreamSource):
    """Plays a Twitch live channel or clip by driving the page's HTML5 video."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_TWITCH_HOST_PATTERN.match(url))

    async def open(self, context: BrowserContext, url: str) -> None:
        log.info("twitch.open", url=url)
        page = await context.new_page()
        self._page = page
        await page.goto(url, wait_until="domcontentloaded")

        # Twitch shows a "Start Watching" / mature-content gate for some channels;
        # best-effort dismiss. Not all channels have it, so this is allowed to fail.
        for selector in (
            'button[data-a-target="content-classification-gate-overlay-start-watching-button"]',
            'button:has-text("Start Watching")',
            'button:has-text("Accept")',
        ):
            try:
                await page.locator(selector).first.click(timeout=2000)
                break
            except Exception:
                continue

        await page.wait_for_selector("video", timeout=20000)

        # Don't autoplay; controller calls play() afterwards.
        await page.evaluate("document.querySelector('video')?.pause()")

        try:
            title = await page.title()
            self._title = title.replace(" - Twitch", "").strip() or None
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
        # Live streams ignore seeks; clips support them.
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

"""YouTube source.

Loads a video via YouTube's embed player URL (``/embed/<id>``) instead
of the watch UI. The embed player has three big advantages for our
capture pipeline:

* No header / search bar / sidebar - we're capturing the X11
  framebuffer, so anything outside the video frame ends up in the
  viewer's stream.
* No EU cookie-consent dialog - that lives on the watch page, not
  the embed.
* Stable autoplay via ``?autoplay=1`` query params; no need to
  programmatically click around the YouTube watch UI.

We still set the ``CONSENT`` cookie pre-emptively as a backup for
edge cases where YouTube serves the consent gate even on embeds
(rare but reported in some EU regions).
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
# Used to pull a video id out of any of the URL forms YouTube uses.
# Matches: youtube.com/watch?v=ID (with optional extra params),
# youtu.be/ID, m.youtube.com/watch?v=ID, youtube.com/embed/ID.
_YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^&]*&)*v=|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def _extract_video_id(url: str) -> str | None:
    match = _YOUTUBE_ID_PATTERN.search(url)
    return match.group(1) if match else None


def _to_embed_url(url: str) -> str:
    """Rewrite a watch URL to the embed equivalent. If the input doesn't
    look like a YouTube URL we recognise, fall back to returning it
    unchanged - the caller will hit the same "no video element" error
    it would have gotten anyway, just with a clearer breadcrumb."""
    video_id = _extract_video_id(url)
    if video_id is None:
        return url
    # autoplay=1 lets us skip the click; mute=0 because we explicitly
    # want audio (the bot routes it through PulseAudio); rel=0 stops
    # YouTube from showing related-video thumbnails after playback.
    return f"https://www.youtube.com/embed/{video_id}?autoplay=1&rel=0"


class YoutubeSource(StreamSource):
    """Plays a YouTube video by driving the page's HTML5 video element."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_YOUTUBE_HOST_PATTERN.match(url))

    async def open(self, context: BrowserContext, url: str) -> None:
        embed_url = _to_embed_url(url)
        log.info("youtube.open", original=url, embed=embed_url)

        # Pre-seed the CONSENT cookie for both YouTube and the parent
        # google.com domain. The embed page normally bypasses the
        # consent dialog entirely, but a few EU regions still gate it
        # - this cookie skips the gate without a click.
        with suppress(Exception):
            await context.add_cookies(
                [
                    {
                        "name": "CONSENT",
                        "value": "YES+",
                        "domain": ".youtube.com",
                        "path": "/",
                    },
                    {
                        "name": "CONSENT",
                        "value": "YES+",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ]
            )

        page = await context.new_page()
        self._page = page
        await page.goto(embed_url, wait_until="domcontentloaded")

        # Defence-in-depth: if a consent banner did slip through, click
        # past it. The embed shouldn't show one but we're paying the
        # cheap price of a 1-second timeout to catch the edge cases.
        with suppress(Exception):
            await page.locator('button:has-text("Accept all")').first.click(timeout=1000)

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

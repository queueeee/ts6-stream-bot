"""Direct file source.

For local media files served via file:// or any plain http(s):// URL that ends in
a known media extension. Renders in a minimal HTML5 <video> wrapper.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from ts6_stream_bot.sources.base import StreamSource

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext

log = structlog.get_logger(__name__)

_MEDIA_EXT_PATTERN = re.compile(
    r"\.(mp4|m4v|mkv|webm|mov|m3u8|mpd|ogg|ogv)(\?.*)?$",
    re.IGNORECASE,
)

_PLAYER_HTML = """
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title>
<style>
  html,body{{margin:0;padding:0;background:#000;height:100%;overflow:hidden}}
  video{{width:100vw;height:100vh;object-fit:contain}}
</style></head>
<body>
  <video id="v" src="{url}" preload="auto" autoplay="false" controls="false"></video>
</body>
</html>
"""


class DirectFileSource(StreamSource):
    """Plays any URL that points directly at a media file."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_MEDIA_EXT_PATTERN.search(url))

    async def open(self, context: BrowserContext, url: str) -> None:
        log.info("directfile.open", url=url)
        page = await context.new_page()
        self._page = page
        await page.set_content(_PLAYER_HTML.format(url=url, title=url), wait_until="domcontentloaded")
        await page.wait_for_selector("video#v", timeout=10000)
        # Don't autoplay - controller calls play() explicitly
        await page.evaluate("document.querySelector('#v').pause()")
        # Use last path segment as a rough title
        self._title = url.rsplit("/", 1)[-1].split("?", 1)[0] or None

    async def play(self) -> None:
        if self._page is None:
            return
        await self._page.evaluate("document.querySelector('#v')?.play()")

    async def pause(self) -> None:
        if self._page is None:
            return
        await self._page.evaluate("document.querySelector('#v')?.pause()")

    async def seek(self, seconds: int) -> None:
        if self._page is None:
            return
        await self._page.evaluate(
            "(s) => { const v = document.querySelector('#v'); if (v) v.currentTime = s; }",
            seconds,
        )

    async def close(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

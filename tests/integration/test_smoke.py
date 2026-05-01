"""Smoke test: spin up the controller, open a trivial URL, take a screenshot, tear down.

Requires:
  * Xvfb running on $DISPLAY (default :99)
  * Chromium installed via `playwright install chromium`
  * RUN_INTEGRATION=1 in the environment

Audio capture / HLS output is NOT exercised here (would require ffmpeg + PulseAudio
plumbing on the runner). That stays the operator's manual smoke check.
"""

from __future__ import annotations

import pytest

from ts6_stream_bot.pipeline.browser import BrowserManager
from ts6_stream_bot.sources.browser_url import BrowserUrlSource


@pytest.mark.asyncio
async def test_browser_opens_and_screenshots() -> None:
    bm = BrowserManager()
    await bm.start()
    try:
        src = BrowserUrlSource()
        await src.open(bm.context, "data:text/html,<h1>hello</h1>")
        assert src.page is not None
        png = await src.page.screenshot(type="png")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        await src.close()
    finally:
        await bm.stop()

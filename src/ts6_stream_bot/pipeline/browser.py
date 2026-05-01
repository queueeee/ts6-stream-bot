"""Playwright browser lifecycle wrapper.

Single browser instance per controller. Headful mode (required so frames hit
the X11 framebuffer where ffmpeg can grab them via x11grab). Hardware
acceleration disabled for the same reason.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog
from playwright.async_api import Browser, BrowserContext, async_playwright

from ts6_stream_bot.config import settings

if TYPE_CHECKING:
    from playwright.async_api import Playwright

log = structlog.get_logger(__name__)


class BrowserManager:
    """Manages the shared Playwright Chromium instance."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def start(self) -> None:
        if self._browser is not None:
            return
        log.info("browser.starting", display=settings.DISPLAY)

        # Make sure Chromium uses our PulseAudio sink
        env = os.environ.copy()
        env["PULSE_SINK"] = settings.PULSE_SINK
        env["DISPLAY"] = settings.DISPLAY

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",                       # required in container
                "--disable-dev-shm-usage",
                "--disable-gpu",                      # CPU rendering -> X11 framebuffer
                "--disable-software-rasterizer",
                "--no-first-run",
                "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                f"--window-size={settings.SCREEN_WIDTH},{settings.SCREEN_HEIGHT}",
                "--window-position=0,0",
                "--start-maximized",
                "--kiosk",                            # fullscreen, no chrome
            ],
            env=env,
        )
        self._context = await self._browser.new_context(
            viewport={"width": settings.SCREEN_WIDTH, "height": settings.SCREEN_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        log.info("browser.ready")

    async def stop(self) -> None:
        log.info("browser.stopping")
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("browser not started")
        return self._context

"""Entrypoint: starts the FastAPI app via uvicorn."""

from __future__ import annotations

import uvicorn

from ts6_stream_bot.config import settings
from ts6_stream_bot.logging_setup import setup_logging


def main() -> None:
    setup_logging(level=settings.LOG_LEVEL)
    uvicorn.run(
        "ts6_stream_bot.api.app:create_app",
        host="0.0.0.0",
        port=8080,
        factory=True,
        log_config=None,  # we use structlog, not uvicorn's logging
        access_log=False,
    )


if __name__ == "__main__":
    main()

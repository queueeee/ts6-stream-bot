"""Simple X-API-Key header check."""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from ts6_stream_bot.config import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Reject requests that don't carry the configured API key."""
    if x_api_key is None or not secrets.compare_digest(x_api_key, settings.BOT_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
        )

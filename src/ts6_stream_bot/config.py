"""Application configuration. Read from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel values we refuse to start with: shipping example .env values.
_INSECURE_API_KEYS = {
    "",
    "changeme",
    "changeme-generate-with-openssl-rand-base64-32",
}


class Settings(BaseSettings):
    """All tunable parameters live here. Add new ones rather than reading env directly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---------------------------------------------------------------
    BOT_API_KEY: str = Field(
        ...,
        description="Secret for control API. Header: X-API-Key.",
    )

    # --- Logging -----------------------------------------------------------
    LOG_LEVEL: str = "INFO"

    # --- Display / capture -------------------------------------------------
    DISPLAY: str = ":99"
    SCREEN_WIDTH: int = 1920
    SCREEN_HEIGHT: int = 1080
    SCREEN_FPS: int = 30

    # --- HLS encoder -------------------------------------------------------
    HLS_OUTPUT_DIR: Path = Path("/var/hls")
    HLS_SEGMENT_DURATION: int = 2
    HLS_PLAYLIST_SIZE: int = 6

    # --- Audio -------------------------------------------------------------
    PULSE_SINK: str = "bot_sink"
    AUDIO_LOUDNORM: bool = Field(
        default=False,
        description="Apply ffmpeg loudnorm filter to normalize audio loudness.",
    )

    # --- Rooms -------------------------------------------------------------
    DEFAULT_ROOM: str = "default"

    @field_validator("BOT_API_KEY")
    @classmethod
    def _reject_insecure_api_key(cls, v: str) -> str:
        if v.strip().lower() in _INSECURE_API_KEYS:
            raise ValueError(
                "BOT_API_KEY is missing or set to a placeholder. "
                "Generate one with: openssl rand -base64 32"
            )
        return v


settings = Settings()  # type: ignore[call-arg]

"""Application configuration. Read from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunable parameters live here. Add new ones rather than reading env directly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---------------------------------------------------------------
    BOT_API_KEY: str = Field(
        default="changeme",
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

    # --- PulseAudio --------------------------------------------------------
    PULSE_SINK: str = "bot_sink"

    # --- Rooms -------------------------------------------------------------
    DEFAULT_ROOM: str = "default"


settings = Settings()

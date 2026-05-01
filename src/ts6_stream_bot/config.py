"""Application configuration. Read from environment / .env file."""

from __future__ import annotations

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
    # 720p30 keeps the per-viewer VP8 encode out of OOM territory on
    # small hosts. aiortc + PyAV at 1080p30 with even one peer can run a
    # 4 GB host out of memory (live deploy hit SIGKILL after ~6 s of
    # streaming). Bump SCREEN_WIDTH/SCREEN_HEIGHT in .env if your host
    # has the budget; you'll also want to bump STREAM_BITRATE.
    DISPLAY: str = ":99"
    SCREEN_WIDTH: int = 1280
    SCREEN_HEIGHT: int = 720
    SCREEN_FPS: int = 30

    # --- Audio -------------------------------------------------------------
    PULSE_SINK: str = "bot_sink"
    AUDIO_LOUDNORM: bool = Field(
        default=False,
        description="Apply ffmpeg loudnorm filter to normalize audio loudness.",
    )

    # --- TS6 connection (used by phase 1+; placeholders for now) -----------
    # The bot will speak the TS3 voice protocol directly to push audio + video
    # into a TS6 channel via the built-in stream feature. These settings are
    # accepted today but not yet wired - phase 0 only owns the public surface.
    TS6_HOST: str = Field(default="", description="TS6 server host (DNS or IP).")
    TS6_PORT: int = Field(default=9987, description="TS6 server voice port (UDP).")
    TS6_NICKNAME: str = Field(default="ts6-stream-bot", description="Nickname the bot shows.")
    TS6_SERVER_PASSWORD: str = Field(default="", description="Server password if any.")
    TS6_DEFAULT_CHANNEL: str = Field(
        default="",
        description="Channel the bot auto-joins on connect (empty = default channel).",
    )
    TS6_CHANNEL_PASSWORD: str = Field(default="", description="Channel password if any.")

    # --- Stream parameters -------------------------------------------------
    # These shape the `setupstream` request the bot sends on connect.
    # Defaults match what the TS6 client UI uses for a normal screen-share.
    STREAM_BITRATE: int = Field(default=4608, description="Stream bitrate hint (kbps).")
    STREAM_ACCESSIBILITY: int = Field(
        default=0,
        description=(
            "0 = public (anyone in the channel can join), "
            "1 = restricted (requires explicit allow per-viewer)."
        ),
    )
    STREAM_MODE: int = Field(
        default=1,
        description=(
            "1 = request-based join (server forwards notifyjoinstreamrequest "
            "to the bot for each viewer; bot replies via "
            "respondjoinstreamrequest). 0 = auto-accept."
        ),
    )
    STREAM_VIEWER_LIMIT: int = Field(
        default=-1,
        description="Max viewers; -1 = unlimited (TS3 convention).",
    )

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

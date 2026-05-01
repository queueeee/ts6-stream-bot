"""Request and response schemas for the control API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ts6_stream_bot.pipeline import StreamState


class PlayRequest(BaseModel):
    url: str = Field(..., description="Source URL to play (YouTube link, mp4, etc.)")


class SeekRequest(BaseModel):
    seconds: int = Field(..., ge=0, description="Absolute target time in seconds")


class StatusResponse(BaseModel):
    state: StreamState
    url: str | None = None
    title: str | None = None
    source_class: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    ok: bool = True
    version: str


class PulseSinkInfo(BaseModel):
    index: int
    name: str
    driver: str
    sample_spec: str
    state: str


class AudioDebugResponse(BaseModel):
    default_sink: str | None = None
    sinks: list[PulseSinkInfo] = Field(default_factory=list)

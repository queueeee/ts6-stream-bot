"""REST endpoints."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from ts6_stream_bot import __version__, metrics
from ts6_stream_bot.api.auth import require_api_key
from ts6_stream_bot.api.schemas import (
    AudioDebugResponse,
    HealthResponse,
    PlayRequest,
    PulseSinkInfo,
    SeekRequest,
    StatusResponse,
)
from ts6_stream_bot.pipeline import StreamController
from ts6_stream_bot.pipeline.audio import get_default_sink, list_sinks

router = APIRouter()


def _controller(request: Request) -> StreamController:
    return request.app.state.controller  # type: ignore[no-any-return]


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.get("/status", response_model=StatusResponse, tags=["control"])
async def get_status(request: Request) -> StatusResponse:
    s = await _controller(request).status()
    metrics.observe_state(s.state.value)
    return StatusResponse(**s.__dict__)


@router.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
async def get_metrics(request: Request) -> PlainTextResponse:
    s = await _controller(request).status()
    metrics.observe_state(s.state.value)
    body, content_type = metrics.render()
    return PlainTextResponse(content=body, media_type=content_type)


@router.post(
    "/play",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def play(req: PlayRequest, request: Request) -> StatusResponse:
    metrics.PLAY_REQUESTS.inc()
    try:
        s = await _controller(request).play(url=req.url, room=req.room)
    except Exception:
        metrics.PLAY_FAILURES.inc()
        raise
    return StatusResponse(**s.__dict__)


@router.get(
    "/debug/screenshot",
    tags=["debug"],
    dependencies=[Depends(require_api_key)],
    responses={
        200: {"content": {"image/png": {}}},
        409: {"description": "no active source"},
    },
)
async def debug_screenshot(request: Request) -> Response:
    """Return a PNG screenshot of the active page. 409 if no source is open."""
    png = await _controller(request).screenshot()
    if png is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no active source",
        )
    return Response(content=png, media_type="image/png")


@router.get(
    "/debug/audio",
    response_model=AudioDebugResponse,
    tags=["debug"],
    dependencies=[Depends(require_api_key)],
)
async def debug_audio() -> AudioDebugResponse:
    """List PulseAudio sinks and the default sink (helps diagnose audio routing)."""
    sinks = await list_sinks()
    default = await get_default_sink()
    return AudioDebugResponse(
        default_sink=default,
        sinks=[PulseSinkInfo(**asdict(s)) for s in sinks],
    )


@router.post(
    "/pause",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def pause(request: Request) -> StatusResponse:
    s = await _controller(request).pause()
    return StatusResponse(**s.__dict__)


@router.post(
    "/resume",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def resume(request: Request) -> StatusResponse:
    s = await _controller(request).resume()
    return StatusResponse(**s.__dict__)


@router.post(
    "/seek",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def seek(req: SeekRequest, request: Request) -> StatusResponse:
    s = await _controller(request).seek(seconds=req.seconds)
    return StatusResponse(**s.__dict__)


@router.post(
    "/stop",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def stop(request: Request) -> StatusResponse:
    s = await _controller(request).stop()
    return StatusResponse(**s.__dict__)

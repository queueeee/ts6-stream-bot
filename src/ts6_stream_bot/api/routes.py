"""REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ts6_stream_bot import __version__
from ts6_stream_bot.api.auth import require_api_key
from ts6_stream_bot.api.schemas import (
    HealthResponse,
    PlayRequest,
    SeekRequest,
    StatusResponse,
)
from ts6_stream_bot.pipeline import StreamController

router = APIRouter()


def _controller(request: Request) -> StreamController:
    return request.app.state.controller  # type: ignore[no-any-return]


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(version=__version__)


@router.get("/status", response_model=StatusResponse, tags=["control"])
async def status(request: Request) -> StatusResponse:
    s = await _controller(request).status()
    return StatusResponse(**s.__dict__)


@router.post(
    "/play",
    response_model=StatusResponse,
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)
async def play(req: PlayRequest, request: Request) -> StatusResponse:
    s = await _controller(request).play(url=req.url, room=req.room)
    return StatusResponse(**s.__dict__)


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

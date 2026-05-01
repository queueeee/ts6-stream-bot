"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ts6_stream_bot.api.routes import router
from ts6_stream_bot.pipeline import SourceOpenError, StreamController

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    """App startup/shutdown: own the StreamController."""
    controller = StreamController()
    await controller.startup()
    app.state.controller = controller
    log.info("app.started")
    try:
        yield
    finally:
        log.info("app.stopping")
        await controller.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title="ts6-stream-bot",
        description="Self-hosted watch-together backend for TeamSpeak 6",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request, exc):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=422,
            content={"error": "validation_failed", "detail": exc.errors()},
        )

    @app.exception_handler(SourceOpenError)
    async def _on_source_open_error(request, exc):  # type: ignore[no-untyped-def]
        # Source/encoder failure is not an internal bug; surface it cleanly so
        # the client can react. /status will also reflect state=idle + error.
        return JSONResponse(
            status_code=502,
            content={"error": "source_open_failed", "detail": str(exc)},
        )

    @app.exception_handler(Exception)
    async def _on_unhandled(request, exc):  # type: ignore[no-untyped-def]
        log.exception("api.unhandled_error", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )

    return app

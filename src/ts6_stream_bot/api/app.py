"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ts6_stream_bot.api.routes import router
from ts6_stream_bot.pipeline import SourceOpenError, StreamController

# Serve the control UI alongside the API. The container places /app/frontend
# next to /app/src; this resolves to the shipped index.html.
_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"

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

    # Mount the control UI at "/" if the directory ships - skip silently
    # otherwise (the project is still useful headlessly).
    if _FRONTEND_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=_FRONTEND_DIR),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def _index() -> FileResponse:
            return FileResponse(_FRONTEND_DIR / "index.html")

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

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, games, replay, stream
from app.core.artifacts import get_baseline, get_model
from app.core.config import settings
from app.core.logging_setup import configure_logging
from app.nba.weekly import get_weekly_service

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_model()
        get_baseline()
        logger.info("Win-probability model and anomaly baseline loaded.")
    except FileNotFoundError as exc:
        logger.warning("%s", exc)
    else:
        if settings.prewarm_week:
            # Start reading this week's games now, so the first visitor after
            # a restart (or a free-tier wake-up) doesn't wait for the build.
            get_weekly_service().snapshot()
    yield


app = FastAPI(title="Heat Check: Live NBA Win-Probability & Anomaly Tracker", lifespan=lifespan)



class GZipExceptStreams:
    """GZipMiddleware for everything except the SSE routes.

    Compression buffers: a compressor holds output back until it has enough
    to be worth emitting, which would delay live events. Starlette's own
    middleware already skips text/event-stream responses by content type;
    this keeps the stream routes out by path as well, so that stays true even
    if that behaviour or a response's content type changes.
    """

    def __init__(self, app, **kwargs) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, **kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].endswith("/stream"):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


# Compresses pages, scripts and JSON (not the live streams, see above).
app.add_middleware(GZipExceptStreams, minimum_size=1000)
# The bundled frontend is served from this same origin and needs no CORS. Set
# CORS_ALLOW_ORIGINS (comma-separated) to let another site call the API.
if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # The traceback goes to the log; the client only learns that it failed.
    logger.error("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


app.include_router(games.router)
app.include_router(stream.router)
app.include_router(replay.router)
app.include_router(dashboard.router)


class FrontendFiles(StaticFiles):
    """Static files that browsers must revalidate before reuse.

    Without a Cache-Control header browsers cache these heuristically, so
    after a deploy a visitor can get the new index.html with an old
    styles.css/main.js and an empty page. "no-cache" still allows the cached
    copy, but only after a cheap ETag check (304) confirms it is current.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.startswith("vendor/"):
            # D3 and the font never change in place (a new version would get
            # a new file name), so browsers can keep them for a week.
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


@app.get("/health")
def health():
    # `commit` is the git commit this instance is running, when the host says
    # (Render sets RENDER_GIT_COMMIT). The deploy workflow compares it with the
    # commit it just tested to know when the new version is live.
    return {"status": "ok", "commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or None}


if FRONTEND_DIR.exists():
    app.mount("/", FrontendFiles(directory=FRONTEND_DIR, html=True), name="frontend")

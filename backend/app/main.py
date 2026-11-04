from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import dashboard, games, replay, stream
from app.core.artifacts import get_baseline, get_model
from app.core.config import settings
from app.nba.weekly import get_weekly_service

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_model()
        get_baseline()
        print("Win-probability model and anomaly baseline loaded.")
    except FileNotFoundError as exc:
        print(f"WARNING: {exc}")
    else:
        if settings.prewarm_week:
            # Start reading this week's games now, so the first visitor after
            # a restart (or a free-tier wake-up) doesn't wait for the build.
            get_weekly_service().snapshot()
    yield


app = FastAPI(title="Heat Check: Live NBA Win-Probability & Anomaly Tracker", lifespan=lifespan)

# Compresses pages, scripts and JSON. Starlette's GZipMiddleware leaves
# text/event-stream (the live SSE feed) alone, so streams aren't buffered.
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    return {"status": "ok"}


if FRONTEND_DIR.exists():
    app.mount("/", FrontendFiles(directory=FRONTEND_DIR, html=True), name="frontend")

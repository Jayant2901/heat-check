from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import games, replay, stream
from app.core.artifacts import get_baseline, get_model

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        get_model()
        get_baseline()
        print("Win-probability model and anomaly baseline loaded.")
    except FileNotFoundError as exc:
        print(f"WARNING: {exc}")
    yield


app = FastAPI(title="Heat Check: Live NBA Win-Probability & Anomaly Tracker", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(games.router)
app.include_router(stream.router)
app.include_router(replay.router)


@app.get("/health")
def health():
    return {"status": "ok"}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

"""Lists available replay fixtures and starts replay sessions.

A replay session is driven by the identical poller/engine code path as a
live game (app/polling/poller.py) -- it just gets a ReplaySource instead of
an NbaLiveDataSource, and a namespaced session key so multiple viewers can
each watch their own playthrough without colliding. Once started, a session
streams through the exact same /api/games/{key}/stream endpoint as a live
game (app/api/stream.py) -- no separate replay-only streaming path exists.
"""

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.artifacts import get_baseline, get_model
from app.core.config import REPLAY_FIXTURES_DIR, settings
from app.datasources.replay_source import ReplaySource
from app.polling.poller import poller_manager

router = APIRouter(prefix="/api/replays", tags=["replay"])


@router.get("")
def list_replays():
    manifest_path = REPLAY_FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"fixtures": []}
    return json.loads(manifest_path.read_text())


@router.post("/{game_id}/start")
async def start_replay(game_id: str, speed: float = 10.0):
    # Must be async: PollerManager.start() calls asyncio.create_task(), which
    # needs a running event loop. A sync `def` here gets offloaded to
    # FastAPI's worker thread pool, which has none -- confirmed directly,
    # this failed with "no running event loop" before the fix.
    fixture_path = REPLAY_FIXTURES_DIR / f"{game_id}.json"
    if not fixture_path.exists():
        raise HTTPException(status_code=404, detail="No such replay fixture")
    if speed <= 0:
        raise HTTPException(status_code=422, detail="speed must be positive")

    active_replays = [k for k in poller_manager.active_keys() if k.startswith("replay:")]
    if len(active_replays) >= settings.max_concurrent_replay_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent replay sessions (max {settings.max_concurrent_replay_sessions}); "
            "try again once one finishes",
        )

    session_id = uuid.uuid4().hex[:8]
    key = f"replay:{game_id}:{session_id}"
    poller_manager.start(
        key,
        ReplaySource(fixture_path),
        get_model(),
        get_baseline(),
        interval_seconds=settings.poll_interval_seconds / speed,
        source_game_id=game_id,
    )
    return {"session_key": key, "stream_url": f"/api/games/{key}/stream"}

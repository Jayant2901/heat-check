"""Lists available replay fixtures and starts replay sessions (for a fixture,
or for any game that finished in the last week).

A replay session is driven by the identical poller/engine code path as a
live game (app/polling/poller.py) -- it just gets a ReplaySource instead of
an NbaLiveDataSource, and a namespaced session key so multiple viewers can
each watch their own playthrough without colliding. Once started, a session
streams through the exact same /api/games/{key}/stream endpoint as a live
game (app/api/stream.py) -- no separate replay-only streaming path exists.
"""

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.artifacts import get_baseline, get_model
from app.core.config import REPLAY_FIXTURES_DIR, settings
from app.datasources.replay_source import ReplaySource
from app.nba.client import get_nba_client
from app.nba.parse import parse_scoreboard
from app.nba.replays import NotReplayable, past_game_snapshots
from app.nba.weekly import get_weekly_service
from app.polling.poller import poller_manager

router = APIRouter(prefix="/api/replays", tags=["replay"])


@router.get("")
def list_replays():
    manifest_path = REPLAY_FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        return {"fixtures": []}
    return json.loads(manifest_path.read_text())


# NBA.com ids are 10 digits, ESPN event ids 9; demo games are "demo-...".
_GAME_ID = re.compile(r"^(\d{9,10}|demo-[\w-]+)$")
WEEK_WAIT_SECONDS = 15.0


async def _find_finished_game(game_id: str) -> dict | None:
    """A game that finished in the dashboard's window, or today: the only
    games a past-game replay will fetch play-by-play for."""
    try:
        service = get_weekly_service()
    except FileNotFoundError:
        service = None
    if service is not None:
        game = service.finished_game(game_id)
        if game is not None:
            return game
    try:
        board = parse_scoreboard(await get_nba_client().scoreboard())
    except Exception:
        board = {"games": []}
    for game in board["games"]:
        if game["game_id"] == game_id and game["status"] == "final":
            return {"game_id": game_id, "home": game["home"]["tricode"], "away": game["away"]["tricode"]}
    if service is not None and not service.ready:
        # The week is still being read (e.g. right after a restart); the game
        # may well be in it, so wait for it briefly rather than failing.
        if await service.wait_ready(WEEK_WAIT_SECONDS):
            return service.finished_game(game_id)
        if not service.ready and service.building:
            raise HTTPException(status_code=503, detail="Still reading this week's games. Try again in a few seconds.")
    return None


@router.post("/{game_id}/start")
async def start_replay(game_id: str, speed: float = 10.0):
    """Start a replay session for a curated fixture or for any game that
    finished in the last week (built from its play-by-play)."""
    # Must be async: PollerManager.start() calls asyncio.create_task(), which
    # needs a running event loop. A sync `def` here gets offloaded to
    # FastAPI's worker thread pool, which has none -- confirmed directly,
    # this failed with "no running event loop" before the fix.
    if speed <= 0:
        raise HTTPException(status_code=422, detail="speed must be positive")

    active_replays = [k for k in poller_manager.active_keys() if k.startswith("replay:")]
    if len(active_replays) >= settings.max_concurrent_replay_sessions:
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent replay sessions (max {settings.max_concurrent_replay_sessions}); "
            "try again once one finishes",
        )

    fixture_path = REPLAY_FIXTURES_DIR / f"{game_id}.json"
    if fixture_path.exists():
        source = ReplaySource(fixture_path)
    else:
        game = await _find_finished_game(game_id) if _GAME_ID.match(game_id) else None
        if game is None:
            raise HTTPException(status_code=404, detail="Not a replay fixture or a game that finished this week")
        try:
            snapshots = await past_game_snapshots(get_nba_client(), game)
        except NotReplayable as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception:
            raise HTTPException(status_code=503, detail="Couldn't load this game's play-by-play. Try again shortly.")
        source = ReplaySource(snapshots=snapshots)

    session_id = uuid.uuid4().hex[:8]
    key = f"replay:{game_id}:{session_id}"
    poller_manager.start(
        key,
        source,
        get_model(),
        get_baseline(),
        interval_seconds=settings.poll_interval_seconds / speed,
        source_game_id=game_id,
    )
    return {"session_key": key, "stream_url": f"/api/games/{key}/stream"}

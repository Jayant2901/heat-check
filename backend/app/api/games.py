import logging

from fastapi import APIRouter, HTTPException

from app.core.game_state import game_state_store
from app.nba.client import get_nba_client
from app.nba.parse import parse_scoreboard
from app.polling.poller import poller_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["games"])

UNREACHABLE = "NBA.com's scoreboard is unreachable right now. Try again in a minute."


async def todays_scoreboard() -> dict:
    """Parsed scoreboard; raises whatever the client raises."""
    return parse_scoreboard(await get_nba_client().scoreboard())


@router.get("/scoreboard")
async def get_scoreboard():
    """Today's games from cdn.nba.com: live first, then upcoming, then final.

    An empty `games` list is normal on days with no games (and in the
    off-season) -- it is not an error.
    """
    client = get_nba_client()
    try:
        board = await todays_scoreboard()
    except Exception:
        logger.warning("Scoreboard fetch failed", exc_info=True)
        raise HTTPException(status_code=503, detail=UNREACHABLE)
    for game in board["games"]:
        game["tracked"] = poller_manager.is_active(game["game_id"])
    return {"mode": client.mode, **board}


@router.get("/games/live")
async def get_live_games():
    """Today's game IDs. Kept for compatibility; /api/scoreboard has more.
    Returns an empty list if NBA.com can't be reached."""
    try:
        board = await todays_scoreboard()
    except Exception:
        logger.warning("Scoreboard fetch failed", exc_info=True)
        return {"game_ids": []}
    return {"game_ids": [g["game_id"] for g in board["games"]]}


@router.get("/games/{game_id}/state")
def get_game_state(game_id: str):
    """Non-streaming snapshot of a tracked game -- used for initial page load
    before the SSE connection (backend/app/api/stream.py) takes over."""
    state = game_state_store.get(game_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Game not tracked")
    return state

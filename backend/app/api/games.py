from fastapi import APIRouter, HTTPException

from app.core.game_state import game_state_store
from app.datasources.nba_live import list_live_game_ids

router = APIRouter(prefix="/api/games", tags=["games"])


@router.get("/live")
async def get_live_games():
    """Today's game IDs from NBA.com's live scoreboard.

    Legitimately returns an empty list during the off-season -- this is
    the correct, testable off-season behavior, not a failure.
    """
    return {"game_ids": await list_live_game_ids()}


@router.get("/{game_id}/state")
def get_game_state(game_id: str):
    """Non-streaming snapshot of a tracked game -- used for initial page load
    before the SSE connection (backend/app/api/stream.py) takes over."""
    state = game_state_store.get(game_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Game not tracked")
    return state

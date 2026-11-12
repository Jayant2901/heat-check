import logging

from fastapi import APIRouter, HTTPException

from app.core.engine import run_payload
from app.core.game_state import game_state_store
from app.nba.client import describe_error, get_nba_client
from app.nba.parse import parse_scoreboard
from app.polling.poller import poller_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["games"])


def unreachable(exc: BaseException) -> str:
    return f"Live NBA data is unreachable right now ({describe_error(exc)}). Try again in a minute."


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
    except Exception as exc:
        logger.warning("Scoreboard fetch failed", exc_info=True)
        raise HTTPException(status_code=503, detail=unreachable(exc))
    for game in board["games"]:
        game["tracked"] = poller_manager.is_active(game["game_id"])
        # Heat checks found so far, for games the server has followed (a live
        # game with viewers, or one it kept after it ended). None = not followed.
        state = game_state_store.get(game["game_id"])
        game["heat_checks"] = None if state is None else len(state.anomalies)
    return {"mode": client.mode, "source": getattr(client, "source", None), **board}


@router.get("/nba-check")
async def nba_check():
    """Diagnostics: can this server reach cdn.nba.com, and what does it say?
    Open it in a browser on a deploy where the dashboard shows "unreachable"."""
    client = get_nba_client()
    if not hasattr(client, "probe"):
        return {"mode": client.mode, "note": "Demo mode does not contact NBA.com.", "checks": []}
    checks = await client.probe()
    return {
        "mode": client.mode,
        "source_in_use": getattr(client, "source", None),
        "ok": any(c["ok"] for c in checks),
        "checks": checks,
    }


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
    """Non-streaming snapshot of a tracked (or recently finished) game."""
    state = game_state_store.get(game_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Game not tracked")
    t = state.score_history[-1].elapsed_seconds if state.score_history else 0.0
    exact = state.fidelity == "play_by_play"
    return {
        "game_id": state.game_id,
        "status": state.status,
        "period": state.period,
        "clock_seconds_remaining": state.clock_seconds_remaining,
        "home_team": state.home_team,
        "away_team": state.away_team,
        "home_score": state.home_score,
        "away_score": state.away_score,
        "fidelity": state.fidelity,
        "wp_home": state.score_history[-1].wp_home if state.score_history else None,
        "points": len(state.score_history),
        "run": run_payload(state.current_run, t, exact),
        "anomalies": [
            {
                "t": a.elapsed_seconds,
                "team": a.team,
                "magnitude": a.magnitude,
                "duration_sec": a.duration_seconds,
                "z_score": a.z_score,
                "bucket": a.bucket,
                "message": a.message,
            }
            for a in state.anomalies
        ],
        "tracked": poller_manager.is_active(game_id),
    }


@router.get("/games/{game_id}/lag")
def get_game_lag(game_id: str):
    """How far behind the real game this server is, measured on the last polls.

    feed_age is how old the newest play in the NBA's feed was when we fetched
    it (the floor: we cannot be faster than the feed). recent_feed_delays are
    server_ts - source_ts for the latest plays pushed, i.e. feed age plus the
    time to the next poll. poll_ms is one whole poll, publish_ms the time to
    run the engine and hand events to subscribers."""
    stats = poller_manager.stats(game_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="Game not tracked")
    delays = stats.get("recent_feed_delays") or []
    stats["median_feed_delay_sec"] = sorted(delays)[len(delays) // 2] if delays else None
    return stats

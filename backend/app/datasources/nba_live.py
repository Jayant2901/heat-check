import asyncio
import logging

from app.core.interfaces import GameSnapshot

logger = logging.getLogger(__name__)

_STATUS_MAP = {1: "scheduled", 2: "live", 3: "final"}


def _parse_game_clock_seconds(iso_clock: str) -> float:
    """Parse nba_api's ISO-8601-duration-ish gameClock, e.g. 'PT08M45.00S'."""
    if not iso_clock or not iso_clock.startswith("PT"):
        return 0.0
    body = iso_clock[2:]
    minutes = 0.0
    seconds = 0.0
    if "M" in body:
        minutes_str, body = body.split("M", 1)
        minutes = float(minutes_str)
    if body.endswith("S"):
        seconds = float(body[:-1] or 0.0)
    return minutes * 60 + seconds


def _snapshot_from_boxscore(game: dict) -> GameSnapshot:
    home = game["homeTeam"]
    away = game["awayTeam"]
    return GameSnapshot(
        game_id=game["gameId"],
        period=game.get("period", 0),
        game_clock_seconds_remaining=_parse_game_clock_seconds(game.get("gameClock", "")),
        home_team=home.get("teamTricode", ""),
        away_team=away.get("teamTricode", ""),
        home_score=home.get("score", 0),
        away_score=away.get("score", 0),
        game_status=_STATUS_MAP.get(game.get("gameStatus", 1), "scheduled"),
    )


class NbaLiveDataSource:
    """Polls NBA.com's live boxscore CDN via nba_api, with retry/backoff.

    Isolated behind the same DataSource protocol as the replay simulator
    (backend/app/datasources/replay_source.py) -- see
    backend/app/core/interfaces.py. This is also the seam a future fallback
    provider (e.g. balldontlie) would slot into without touching the engine,
    model, anomaly, or SSE layers.
    """

    def __init__(self, max_backoff_seconds: float = 60.0) -> None:
        self._max_backoff_seconds = max_backoff_seconds
        self._backoff_seconds: dict[str, float] = {}

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        from nba_api.live.nba.endpoints import boxscore

        try:
            box = await asyncio.to_thread(boxscore.BoxScore, game_id=game_id, timeout=10)
            game = box.game.get_dict()
            self._backoff_seconds[game_id] = 0.0
            return _snapshot_from_boxscore(game)
        except Exception:
            backoff = min(self._backoff_seconds.get(game_id, 1.0) * 2, self._max_backoff_seconds)
            self._backoff_seconds[game_id] = backoff
            logger.warning(
                "Live boxscore fetch failed for game_id=%s; backing off %.0fs",
                game_id,
                backoff,
                exc_info=True,
            )
            return None


async def list_live_game_ids() -> list[str]:
    """Today's in-progress/scheduled game IDs, from nba_api's live scoreboard."""
    from nba_api.live.nba.endpoints import scoreboard

    def _fetch() -> list[str]:
        board = scoreboard.ScoreBoard()
        data = board.get_dict()
        return [g["gameId"] for g in data.get("scoreboard", {}).get("games", [])]

    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        logger.warning("Live scoreboard fetch failed", exc_info=True)
        return []

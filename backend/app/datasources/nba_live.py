"""Live DataSource: turns NBA.com's live boxscore into a GameSnapshot.

Isolated behind the same DataSource protocol as the replay simulator
(app/datasources/replay_source.py) -- see app/core/interfaces.py. The fetch
itself is injected (normally HttpNbaClient.boxscore, see app/nba/client.py),
so this module only knows the payload shape, not how it is fetched.
"""

import logging
from collections.abc import Awaitable, Callable

from app.core.interfaces import GameSnapshot
from app.nba.parse import STATUS_BY_CODE, parse_clock_seconds

logger = logging.getLogger(__name__)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def snapshot_from_boxscore(game: dict) -> GameSnapshot:
    """`game` is the boxscore payload's top-level "game" object."""
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    return GameSnapshot(
        game_id=game.get("gameId", ""),
        period=_int(game.get("period")),
        game_clock_seconds_remaining=parse_clock_seconds(game.get("gameClock")),
        home_team=home.get("teamTricode", ""),
        away_team=away.get("teamTricode", ""),
        home_score=_int(home.get("score")),
        away_score=_int(away.get("score")),
        game_status=STATUS_BY_CODE.get(_int(game.get("gameStatus")) or 1, "scheduled"),
    )


class NbaLiveDataSource:
    """One boxscore fetch per poll. Errors return None; the poller
    (app/polling/poller.py) owns backoff and the "degraded" event."""

    def __init__(self, fetch_boxscore: Callable[[str], Awaitable[dict]]) -> None:
        self._fetch_boxscore = fetch_boxscore

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        try:
            raw = await self._fetch_boxscore(game_id)
            return snapshot_from_boxscore(raw["game"])
        except Exception:
            logger.warning("Live boxscore fetch failed for game_id=%s", game_id, exc_info=True)
            return None

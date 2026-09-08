from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class GameSnapshot:
    """One poll tick's worth of game state -- the only shape the engine ever sees.

    Live and replay sources both produce this; the engine and everything
    downstream of it never knows or cares which one it came from.
    """

    game_id: str
    period: int
    game_clock_seconds_remaining: float
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    game_status: str  # "scheduled" | "live" | "final"


class DataSource(Protocol):
    """Implemented identically by live polling and by the replay simulator.

    This is the seam that lets the replay simulator run the exact same
    engine code as live polling, and the seam that would let a future
    fallback data provider be swapped in without touching the engine,
    model, anomaly, or SSE layers.
    """

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        """Return the latest snapshot for game_id, or None if unavailable this tick."""
        ...

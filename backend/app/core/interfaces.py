from dataclasses import dataclass
from typing import Protocol

# How finely a game's scoring was observed. Shown in every wp_update so the UI
# (and anyone reading the API) can tell exact timing from an approximation.
FIDELITY_PLAY_BY_PLAY = "play_by_play"
FIDELITY_BOXSCORE_TICKS = "boxscore_ticks"


@dataclass(frozen=True)
class GameSnapshot:
    """One observed state of a game -- the only shape the engine ever sees.

    Live and replay sources both produce this; the engine and everything
    downstream of it never knows or cares which one it came from.

    `exact` says how the snapshot was made. True: it is the state right after
    one play-by-play action, so any score change since the previous snapshot
    is one scoring play with its true clock. False: it is a boxscore poll, so
    several plays may have happened since the previous snapshot and their
    order and timing are unknown (lower fidelity; see core/engine.py).
    """

    game_id: str
    period: int
    game_clock_seconds_remaining: float
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    game_status: str  # "scheduled" | "live" | "final"
    exact: bool = False
    # When the feed says this play happened (epoch seconds, from timeActual /
    # wallclock). None for replays and boxscore polls. Used to report feed delay.
    source_ts: float | None = None
    action_number: int | None = None


@dataclass(frozen=True)
class PollInfo:
    """Facts about the most recent poll, for /api/games/{id}/lag."""

    fetch_ms: float = 0.0
    not_modified: bool = False
    backfill: bool = False
    feed_age_seconds: float | None = None
    new_actions: int = 0
    fidelity: str = FIDELITY_PLAY_BY_PLAY


class DataSource(Protocol):
    """Implemented identically by live polling and by the replay simulator.

    This is the seam that lets the replay simulator run the exact same
    engine code as live polling, and the seam that would let a future
    fallback data provider be swapped in without touching the engine,
    model, anomaly, or SSE layers.

    Optional attributes the poller looks for with getattr():
      finished        -- True once a replay has nothing more to give
      backfills       -- True if a fresh instance rebuilds the whole game from
                         the start, so existing state for the key is reset
      last_poll       -- a PollInfo about the latest poll
      suggest_interval(base) -- seconds until the next poll (adaptive cadence)
    """

    async def next_snapshots(self, game_id: str) -> list[GameSnapshot] | None:
        """New snapshots since the last call, oldest first. An empty list
        means nothing new this time; None means the poll failed."""
        ...

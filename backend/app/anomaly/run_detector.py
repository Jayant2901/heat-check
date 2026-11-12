"""Tracks the currently active scoring run for one game, one score event at a time.

A run extends while the same team keeps scoring; it closes the moment the
other team scores, or if too long passes with no score at all (nobody is
"on a run" during a stall -- see gap_seconds).

The detector itself is exact: it is told each scoring play with its time.
How exact the answer is depends on who calls it. Fed from play-by-play (one
scoring play at a time, true clock) it matches how the baseline was built
(backend/historical/build_baseline.py). Fed from boxscore polls it can only
see the net change between polls -- see core/engine.py.
"""

from dataclasses import dataclass

from app.core.game_state import RunState

DEFAULT_GAP_SECONDS = 120.0


@dataclass
class RunUpdate:
    current_run: RunState
    closed_run: RunState | None


class RunDetector:
    def __init__(self, gap_seconds: float = DEFAULT_GAP_SECONDS):
        self.gap_seconds = gap_seconds

    def is_active(self, current: RunState | None, elapsed_seconds: float) -> bool:
        """A run is still 'on' until the gap with no scoring runs out."""
        return current is not None and elapsed_seconds - current.last_scored_elapsed <= self.gap_seconds

    def on_score(
        self,
        current: RunState | None,
        team: str,
        points: int,
        elapsed_seconds: float,
        score_diff_after: int,
        period: int = 0,
        clock: float = 0.0,
    ) -> RunUpdate:
        if (
            current is not None
            and current.team == team
            and elapsed_seconds - current.last_scored_elapsed <= self.gap_seconds
        ):
            current.points_scored += points
            current.last_scored_elapsed = elapsed_seconds
            return RunUpdate(current_run=current, closed_run=None)

        closed = current  # opponent scored, or the gap timed the run out
        new_run = RunState(
            team=team,
            start_elapsed_seconds=elapsed_seconds,
            start_score_diff=score_diff_after,
            points_scored=points,
            last_scored_elapsed=elapsed_seconds,
            start_period=period,
            start_clock=clock,
        )
        return RunUpdate(current_run=new_run, closed_run=closed)

    def reset(self, current: RunState | None) -> RunState | None:
        """Called when a tick can't be attributed to one team (see
        core/engine.py) -- the ambiguity breaks any run in progress."""
        return None

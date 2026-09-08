"""The live-parity feature contract.

The live pipeline only ever has boxscore-level fields -- score, period,
clock -- never play-by-play. So even though the historical pipeline builds
its training rows from play-by-play, the feature *set* itself must be
restricted to exactly what's derivable live. This module is that boundary:
train.py and win_prob.py both import FEATURE_COLUMNS and build_feature_row
from here rather than each defining their own, so the model can never
silently end up depending on a field only the offline pipeline has.
"""

REGULATION_PERIODS = 4
REGULATION_PERIOD_SECONDS = 12 * 60
OT_PERIOD_SECONDS = 5 * 60

FEATURE_COLUMNS = ["margin", "time_remaining_seconds", "period"]


def elapsed_seconds(period: int, clock_seconds_remaining: float) -> float:
    """Total game-elapsed seconds, regulation + any completed OT periods."""
    if period <= REGULATION_PERIODS:
        completed = (period - 1) * REGULATION_PERIOD_SECONDS
        period_length = REGULATION_PERIOD_SECONDS
    else:
        completed = REGULATION_PERIODS * REGULATION_PERIOD_SECONDS + (period - REGULATION_PERIODS - 1) * OT_PERIOD_SECONDS
        period_length = OT_PERIOD_SECONDS
    return completed + (period_length - clock_seconds_remaining)


def time_remaining_seconds(period: int, clock_seconds_remaining: float) -> float:
    """Seconds remaining in regulation; during/after OT this is just the
    current OT period's clock -- `period` itself is a separate feature so
    the model can learn OT dynamics distinctly rather than pretending
    regulation time is still ticking down."""
    if period <= REGULATION_PERIODS:
        periods_left_after_this = REGULATION_PERIODS - period
        return clock_seconds_remaining + periods_left_after_this * REGULATION_PERIOD_SECONDS
    return clock_seconds_remaining


def build_feature_row(margin: int, time_remaining: float, period: int) -> dict:
    return {"margin": margin, "time_remaining_seconds": time_remaining, "period": period}

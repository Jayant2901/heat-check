"""Loads the run-magnitude/time-elapsed baseline table and z-scores a live run.

Schema (produced by backend/historical/build_baseline.py, one row per
(period, time_bucket, magnitude)):
    period        int    -- 1-4 regulation, 5+ OT
    time_bucket   int    -- seconds remaining IN that period, floored to a
                             5-minute window (e.g. 300 means "5:00-9:59 left")
    magnitude     int    -- run size in points (unanswered points by one team)
    mean_duration float  -- mean seconds it historically took a run of this
                             size to happen, at this point in the game
    std_duration  float
    n             int    -- sample size backing this bucket

A run's anomaly z-score is (mean_duration - observed_duration) / std_duration,
so a HIGHER z means the run happened FASTER than history says runs of that
size usually do at that point in the game -- i.e. more anomalous. Buckets
with too few samples fall back to (period, magnitude) pooled across all
time_buckets, then to (magnitude) pooled globally; the fallback used is
returned alongside the z-score so callers know how approximate it is.
"""

from pathlib import Path

import pandas as pd

from app.core.game_state import RunState

TIME_BUCKET_SECONDS = 300
MIN_SAMPLES = 20


def load_baseline(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"No run baseline table at {path}. Run backend/historical/build_baseline.py "
            "(after fetch_games.py + fetch_pbp.py) to produce it."
        )
    return pd.read_parquet(path)


def time_bucket(clock_seconds_remaining: float) -> int:
    return int(clock_seconds_remaining // TIME_BUCKET_SECONDS) * TIME_BUCKET_SECONDS


def _lookup(baseline: pd.DataFrame, period: int, bucket: int, magnitude: int):
    exact = baseline[
        (baseline["period"] == period) & (baseline["time_bucket"] == bucket) & (baseline["magnitude"] == magnitude)
    ]
    if len(exact) and exact["n"].iloc[0] >= MIN_SAMPLES:
        row = exact.iloc[0]
        return row["mean_duration"], row["std_duration"], "exact"

    by_period = baseline[(baseline["period"] == period) & (baseline["magnitude"] == magnitude)]
    if len(by_period):
        agg = by_period[["mean_duration", "std_duration", "n"]].mean()
        if agg["n"] >= MIN_SAMPLES:
            return agg["mean_duration"], agg["std_duration"], "period_pooled"

    by_magnitude = baseline[baseline["magnitude"] == magnitude]
    if len(by_magnitude):
        agg = by_magnitude[["mean_duration", "std_duration", "n"]].mean()
        if agg["n"] > 0:
            return agg["mean_duration"], agg["std_duration"], "global"

    return None


def z_score_run(
    baseline: pd.DataFrame,
    run: RunState,
    elapsed_seconds: float,
    clock_seconds_remaining: float,
    period: int,
) -> tuple[float, str] | None:
    duration = elapsed_seconds - run.start_elapsed_seconds
    if duration <= 0:
        return None
    looked_up = _lookup(baseline, period, time_bucket(clock_seconds_remaining), run.points_scored)
    if looked_up is None:
        return None
    mean_duration, std_duration, bucket_label = looked_up
    if std_duration <= 0:
        return None
    return (mean_duration - duration) / std_duration, bucket_label

import pandas as pd
import pytest

from app.anomaly.baseline import z_score_run
from app.anomaly.run_detector import RunDetector
from app.core.game_state import RunState


def test_run_extends_while_same_team_scores():
    rd = RunDetector(gap_seconds=120)
    update = rd.on_score(None, "home", 2, elapsed_seconds=100, score_diff_after=2)
    assert update.current_run.points_scored == 2
    assert update.closed_run is None

    update = rd.on_score(update.current_run, "home", 3, elapsed_seconds=130, score_diff_after=5)
    assert update.current_run.points_scored == 5
    assert update.closed_run is None


def test_run_closes_when_opponent_scores():
    rd = RunDetector(gap_seconds=120)
    current = rd.on_score(None, "home", 8, elapsed_seconds=100, score_diff_after=8).current_run

    update = rd.on_score(current, "away", 2, elapsed_seconds=140, score_diff_after=6)
    assert update.closed_run is current
    assert update.current_run.team == "away"
    assert update.current_run.points_scored == 2


def test_run_closes_after_a_long_scoring_gap():
    rd = RunDetector(gap_seconds=60)
    current = rd.on_score(None, "home", 4, elapsed_seconds=100, score_diff_after=4).current_run

    # same team scores again, but 200s later -- gap timeout, not an extension
    update = rd.on_score(current, "home", 2, elapsed_seconds=300, score_diff_after=6)
    assert update.closed_run is current
    assert update.current_run.points_scored == 2
    assert update.current_run.start_elapsed_seconds == 300


@pytest.fixture
def baseline_df():
    import math

    # mean_log_duration = log(90) etc -- a "typical" 8-point run here takes
    # about 90s (period=4, bucket=300); std chosen in log-space directly
    # since that's the space z-scoring happens in (see baseline.py).
    return pd.DataFrame(
        [
            # exact bucket for an 8-point run late in Q4 with 10:00-14:59 remaining is missing above,
            # so this exercises the fallback chain explicitly.
            {
                "period": 4,
                "time_bucket": 300,
                "magnitude": 8,
                "mean_log_duration": math.log(90.0),
                "std_log_duration": 0.3,
                "n": 50,
            },
            {
                "period": 4,
                "time_bucket": 0,
                "magnitude": 8,
                "mean_log_duration": math.log(100.0),
                "std_log_duration": 0.3,
                "n": 5,
            },
            {
                "period": 1,
                "time_bucket": 600,
                "magnitude": 8,
                "mean_log_duration": math.log(120.0),
                "std_log_duration": 0.3,
                "n": 40,
            },
            {
                "period": 2,
                "time_bucket": 600,
                "magnitude": 8,
                "mean_log_duration": math.log(110.0),
                "std_log_duration": 0.3,
                "n": 40,
            },
        ]
    )


def test_z_score_exact_bucket_hit(baseline_df):
    run = RunState(team="home", start_elapsed_seconds=0, start_score_diff=0, points_scored=8)
    # duration = 40s, much faster than the ~90s a typical run of this size takes
    z, label = z_score_run(baseline_df, run, elapsed_seconds=40, clock_seconds_remaining=310, period=4)
    assert label == "exact"
    assert z > 0  # faster than typical -> more anomalous, positive by convention


def test_z_score_falls_back_when_bucket_too_sparse(baseline_df):
    run = RunState(team="home", start_elapsed_seconds=0, start_score_diff=0, points_scored=8)
    # (period=4, time_bucket=0) has n=5, below MIN_SAMPLES -- must fall back
    z, label = z_score_run(baseline_df, run, elapsed_seconds=50, clock_seconds_remaining=5, period=4)
    assert label in ("period_pooled", "global")


def test_z_score_returns_none_for_unseen_magnitude(baseline_df):
    run = RunState(team="home", start_elapsed_seconds=0, start_score_diff=0, points_scored=99)
    assert z_score_run(baseline_df, run, elapsed_seconds=50, clock_seconds_remaining=300, period=4) is None

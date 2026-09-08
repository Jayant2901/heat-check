import math

import pandas as pd
import pytest

from app.core.engine import process_tick
from app.core.game_state import GameState
from app.core.interfaces import GameSnapshot


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]]


@pytest.fixture
def baseline_df():
    # a typical 8-point run here takes 90s; std_log_duration is picked so
    # that a 30s run (see test_run_builds_and_flags_anomaly) lands at
    # exactly z=3.0: (log(90)-log(30))/std = log(3)/std = 3.0 -> std = log(3)/3
    return pd.DataFrame(
        [
            {
                "period": 4,
                "time_bucket": 300,
                "magnitude": 8,
                "mean_log_duration": math.log(90.0),
                "std_log_duration": math.log(3.0) / 3.0,
                "n": 50,
            },
        ]
    )


def snapshot(period, clock, home, away, status="live"):
    return GameSnapshot(
        game_id="TEST1",
        period=period,
        game_clock_seconds_remaining=clock,
        home_team="HOM",
        away_team="AWY",
        home_score=home,
        away_score=away,
        game_status=status,
    )


def test_wp_update_emitted_every_tick(baseline_df):
    state = GameState(game_id="TEST1")
    events = process_tick(state, snapshot(4, 560, 80, 80), FakeModel(), baseline_df)
    assert [e.type for e in events] == ["wp_update"]
    assert state.score_history[-1].home_score == 80


def test_run_builds_and_flags_anomaly(baseline_df):
    state = GameState(game_id="TEST1")
    model = FakeModel()

    process_tick(state, snapshot(4, 560, 80, 80), model, baseline_df)
    events = process_tick(state, snapshot(4, 550, 88, 80), model, baseline_df)
    assert state.current_run is not None
    assert state.current_run.points_scored == 8
    assert [e.type for e in events] == ["wp_update"]  # duration==0 right when the run starts -> no anomaly yet

    events = process_tick(state, snapshot(4, 520, 88, 80), model, baseline_df)
    types = [e.type for e in events]
    assert types == ["wp_update", "anomaly"]
    anomaly = events[1]
    assert anomaly.team == "home"
    assert anomaly.magnitude == 8
    assert anomaly.z_score == pytest.approx(3.0)
    assert anomaly.bucket == "exact"
    assert state.current_run.anomaly_flagged is True

    # a flagged run doesn't re-fire on subsequent ticks
    events = process_tick(state, snapshot(4, 500, 88, 80), model, baseline_df)
    assert [e.type for e in events] == ["wp_update"]


def test_opponent_score_closes_the_run(baseline_df):
    state = GameState(game_id="TEST1")
    model = FakeModel()

    process_tick(state, snapshot(4, 560, 80, 80), model, baseline_df)
    process_tick(state, snapshot(4, 550, 88, 80), model, baseline_df)
    process_tick(state, snapshot(4, 540, 88, 83), model, baseline_df)

    assert state.current_run.team == "away"
    assert state.current_run.points_scored == 3


def test_both_teams_scoring_in_one_tick_resets_the_run(baseline_df):
    state = GameState(game_id="TEST1")
    model = FakeModel()

    process_tick(state, snapshot(4, 560, 80, 80), model, baseline_df)
    process_tick(state, snapshot(4, 550, 88, 80), model, baseline_df)
    process_tick(state, snapshot(4, 540, 90, 82), model, baseline_df)  # both teams scored this tick

    assert state.current_run is None


def test_game_end_event_on_final_status(baseline_df):
    state = GameState(game_id="TEST1")
    model = FakeModel()

    events = process_tick(state, snapshot(4, 0, 100, 90, status="final"), model, baseline_df)
    end_events = [e for e in events if e.type == "game_end"]
    assert len(end_events) == 1
    assert end_events[0].winner == "HOM"
    assert end_events[0].final_home_score == 100

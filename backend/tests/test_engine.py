import json
import math

import pandas as pd
import pytest

from app.core.config import REPLAY_FIXTURES_DIR
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


def test_pregame_snapshot_is_treated_as_the_opening_tip(baseline_df):
    state = GameState(game_id="TEST1")
    events = process_tick(state, snapshot(0, 0, 0, 0, status="scheduled"), FakeModel(), baseline_df)
    wp = events[0]
    assert wp.t == 0
    assert wp.period == 1 and wp.clock == 720
    assert wp.status == "scheduled"
    assert state.score_history[-1].elapsed_seconds == 0


def test_anomaly_message_uses_the_team_code(baseline_df):
    state = GameState(game_id="TEST1")
    model = FakeModel()
    process_tick(state, snapshot(4, 560, 80, 80), model, baseline_df)
    process_tick(state, snapshot(4, 550, 88, 80), model, baseline_df)
    events = process_tick(state, snapshot(4, 520, 88, 80), model, baseline_df)
    anomaly = events[1]
    assert anomaly.message.startswith("HOM just scored 8 unanswered points in 30 seconds")
    assert anomaly.team == "home"  # the event field stays home/away


def test_mild_z_scores_get_mild_wording(baseline_df):
    from app.core.engine import _describe_rarity

    tier, phrase = _describe_rarity(1.6, seed=0)
    assert tier == "mild"
    assert "rare" not in phrase
    assert _describe_rarity(2.6, seed=0)[0] == "mid"


def test_sampling_rate_changes_what_gets_flagged():
    """Replay fixtures tick every ~3 s; live polls every 12 s. On this real
    game the engine flags nothing at replay cadence and one run at roughly
    live cadence -- so replay is not a faithful preview of live flags."""
    from app.core.artifacts import get_baseline, get_model

    model, baseline = get_model(), get_baseline()
    data = json.loads((REPLAY_FIXTURES_DIR / "0022200001.json").read_text())
    snapshots = [GameSnapshot(**s) for s in data["snapshots"]]

    def flags(stride):
        sampled = snapshots[::stride]
        if sampled[-1] is not snapshots[-1]:
            sampled.append(snapshots[-1])
        state = GameState(game_id="gap")
        for snap in sampled:
            process_tick(state, snap, model, baseline)
        return len(state.anomalies)

    replay_flags, live_like_flags = flags(1), flags(4)
    assert replay_flags != live_like_flags
    assert (replay_flags, live_like_flags) == (0, 1)

import json
import math
from dataclasses import replace

import pandas as pd
import pytest

from app.core.config import REPLAY_FIXTURES_DIR
from app.core.engine import process_snapshots, process_tick
from app.core.game_state import GameState
from app.core.interfaces import GameSnapshot


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]] * len(X)


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


def _fixture_snapshots(name, exact):
    data = json.loads((REPLAY_FIXTURES_DIR / f"{name}.json").read_text())
    return [GameSnapshot(**{**s, "exact": exact}) for s in data["snapshots"]]


def _run_flags(snapshots, chunk, model, baseline):
    """Feed `snapshots` to the engine `chunk` at a time (a poll that sees
    `chunk` new plays) and return the heat checks it found."""
    state = GameState(game_id="cadence")
    for i in range(0, len(snapshots), chunk):
        process_snapshots(state, snapshots[i : i + chunk], model, baseline, now=0.0)
    return [(round(a.elapsed_seconds, 1), a.magnitude, round(a.duration_seconds, 1), round(a.z_score, 4)) for a in state.anomalies]


def test_play_by_play_flags_do_not_depend_on_polling_cadence():
    """The inverse of the old sampling test. With one exact snapshot per play,
    how many plays a poll delivers (1 each, a 3 s feed's handful, a 12 s
    feed's dozen, the whole game at once) changes nothing: same runs, same
    times, same z-scores."""
    from app.core.artifacts import get_baseline, get_model

    model, baseline = get_model(), get_baseline()
    snapshots = _fixture_snapshots("0022301034", exact=True)  # the 30-point comeback: three known runs
    reference = _run_flags(snapshots, 1, model, baseline)
    assert len(reference) == 3
    for chunk in (2, 5, 12, 60, len(snapshots)):
        assert _run_flags(snapshots, chunk, model, baseline) == reference


def test_boxscore_ticks_depend_on_sampling():
    """The lower-fidelity path (exact=False) keeps its documented weakness:
    the same game gives different flags at different sampling strides."""
    from app.core.artifacts import get_baseline, get_model

    model, baseline = get_model(), get_baseline()
    snapshots = _fixture_snapshots("0022200001", exact=False)

    def flags(stride):
        sampled = snapshots[::stride]
        if sampled[-1] is not snapshots[-1]:
            sampled.append(snapshots[-1])
        state = GameState(game_id="gap")
        for snap in sampled:
            process_tick(state, snap, model, baseline)
        return len(state.anomalies)

    assert (flags(1), flags(4)) == (0, 1)


def test_backfilled_state_equals_streamed_state():
    """A viewer arriving mid-game gets state rebuilt from the play-by-play so
    far in one go; it must equal what streaming play by play produced."""
    from app.core.artifacts import get_baseline, get_model

    model, baseline = get_model(), get_baseline()
    snapshots = _fixture_snapshots("0022301034", exact=True)
    cut = len(snapshots) // 2

    streamed = GameState(game_id="a")
    for snap in snapshots[:cut]:
        process_snapshots(streamed, [snap], model, baseline, now=1.0)
    backfilled = GameState(game_id="b")
    process_snapshots(backfilled, snapshots[:cut], model, baseline, now=1.0)

    assert streamed.score_history == backfilled.score_history
    assert streamed.anomalies == backfilled.anomalies
    assert streamed.current_run == backfilled.current_run
    assert [(e.seq, e.type) for e in streamed.log] == [(e.seq, e.type) for e in backfilled.log]
    assert streamed.log == backfilled.log  # identical events, payload for payload

    # ...and carrying on from either one reaches the same end state
    for state in (streamed, backfilled):
        process_snapshots(state, snapshots[cut:], model, baseline, now=1.0)
    assert streamed.log == backfilled.log


def test_a_redelivered_play_is_ignored():
    state = GameState(game_id="dup")
    snaps = [replace(snapshot(1, 700 - 10 * n, 2 * n, 0), exact=True, action_number=n + 1) for n in range(4)]
    empty = pd.DataFrame([], columns=["period", "time_bucket", "magnitude", "mean_log_duration", "std_log_duration", "n"])
    process_snapshots(state, snaps, FakeModel(), empty, now=0.0)
    events = process_snapshots(state, snaps[1:], FakeModel(), empty, now=0.0)  # retried poll
    assert events == []
    assert len(state.score_history) == 4


def exact(period, clock, home, away, number, status="live", source_ts=None):
    return replace(snapshot(period, clock, home, away, status), exact=True, action_number=number, source_ts=source_ts)


def test_every_wp_update_carries_the_run_state(baseline_df):
    state = GameState(game_id="TEST1")
    steps = [
        exact(4, 600, 80, 80, 1),
        exact(4, 590, 82, 80, 2),  # home +2: a run starts
        exact(4, 580, 84, 80, 3),
        exact(4, 570, 88, 80, 4),  # 8-0
        exact(4, 565, 88, 82, 5),  # away scores: home's run is over, away's begins
    ]
    events = []
    for step in steps:
        events += process_snapshots(state, [step], FakeModel(), baseline_df, now=0.0)
    wp = [e for e in events if e.type == "wp_update"]
    assert len(wp) == 5  # one per play, and every one has a `run` key
    assert wp[0].run is None  # no run yet
    assert wp[1].run == {**wp[1].run, "team": "home", "points": 2, "duration_sec": 0.0}
    assert wp[3].run["points"] == 8 and wp[3].run["duration_sec"] == 20.0
    assert wp[3].run["threshold"] == pytest.approx(1.5)
    assert wp[4].run["team"] == "away" and wp[4].run["points"] == 2


def test_run_state_shows_z_and_flag_when_the_run_is_flagged(baseline_df):
    state = GameState(game_id="TEST1")
    # 8 points in 25 s (first basket to last) at 4th-quarter 9:45 -- a z of 3.5 in this baseline
    steps = [exact(4, 600, 80, 80, 1), exact(4, 585, 82, 80, 2), exact(4, 570, 86, 80, 3), exact(4, 560, 88, 80, 4)]
    events = []
    for step in steps:
        events += process_snapshots(state, [step], FakeModel(), baseline_df, now=0.0)
    wp = [e for e in events if e.type == "wp_update"]
    anomalies = [e for e in events if e.type == "anomaly"]
    assert len(anomalies) == 1
    assert wp[-1].run["flagged"] is True
    expected_z = math.log(90.0 / 25.0) / (math.log(3.0) / 3.0)
    assert wp[-1].run["z_score"] == pytest.approx(expected_z, abs=0.01)
    assert anomalies[0].tier == "high" and anomalies[0].team_code == "HOM"
    assert (anomalies[0].period, anomalies[0].clock) == (4, 560)


def test_run_is_none_after_the_gap_with_no_scoring(baseline_df):
    state = GameState(game_id="TEST1")
    events = []
    for step in (exact(4, 600, 80, 80, 1), exact(4, 590, 88, 80, 2), exact(4, 400, 88, 80, 3)):
        events += process_snapshots(state, [step], FakeModel(), baseline_df, now=0.0)
    assert events[1].run["points"] == 8
    assert events[2].run is None  # 190 s without a basket: nobody is "on a run"


def test_runs_are_scored_where_they_started_like_the_baseline(baseline_df):
    """The baseline buckets a run by the clock when it STARTED. This run
    starts at 5:10 left (bucket 300) and finishes at 4:55 (bucket 0). Looking
    up the finishing clock finds no row and falls back to a pooled average
    with a different label and value; the start bucket gives the exact row."""
    state = GameState(game_id="TEST1")
    events = []
    for step in (exact(4, 320, 80, 80, 1), exact(4, 310, 84, 80, 2), exact(4, 295, 88, 80, 3)):
        events += process_snapshots(state, [step], FakeModel(), baseline_df, now=0.0)
    anomalies = [e for e in events if e.type == "anomaly"]
    assert len(anomalies) == 1
    assert anomalies[0].bucket == "exact"
    assert (state.current_run.start_period, state.current_run.start_clock) == (4, 310)


def test_first_snapshot_of_a_game_in_progress_starts_no_run(baseline_df):
    state = GameState(game_id="TEST1")
    process_snapshots(state, [snapshot(3, 400, 61, 40)], FakeModel(), baseline_df)
    assert state.current_run is None


def test_events_have_increasing_sequence_numbers_and_timestamps(baseline_df):
    state = GameState(game_id="TEST1")
    events = process_snapshots(
        state,
        [exact(1, 700, 0, 0, 1, source_ts=100.0), exact(1, 690, 2, 0, 2, source_ts=110.0)],
        FakeModel(),
        baseline_df,
        live=True,
        now=113.5,
    )
    assert [e.seq for e in events] == [1, 2]
    assert [e.feed_delay_sec for e in events] == [13.5, 3.5]
    assert all(e.server_ts == 113.5 for e in events)
    assert [e.seq for e in state.log] == [1, 2]


def test_feed_delay_is_not_reported_for_backfilled_history(baseline_df):
    state = GameState(game_id="TEST1")
    events = process_snapshots(state, [exact(1, 700, 0, 0, 1, source_ts=100.0)], FakeModel(), baseline_df, live=False, now=9999.0)
    assert events[0].feed_delay_sec is None
    assert events[0].source_ts == 100.0


def test_game_end_is_emitted_once(baseline_df):
    state = GameState(game_id="TEST1")
    final = snapshot(4, 0, 100, 90, status="final")
    first = process_snapshots(state, [final], FakeModel(), baseline_df)
    second = process_snapshots(state, [final], FakeModel(), baseline_df)
    assert [e.type for e in first] == ["wp_update", "game_end"]
    assert [e.type for e in second] == ["wp_update"]


def test_the_reason_a_run_was_not_flagged_is_logged(baseline_df, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="app.core.engine")
    state = GameState(game_id="TEST1")
    # a 3-point run is below the 6-point minimum; an 8-point run in 90 s is z=0
    for step in (exact(4, 600, 80, 80, 1), exact(4, 595, 83, 80, 2)):
        process_snapshots(state, [step], FakeModel(), baseline_df)
    assert "below minimum" in caplog.text
    state = GameState(game_id="TEST2")
    for step in (exact(4, 600, 80, 80, 1), exact(4, 595, 84, 80, 2), exact(4, 510, 88, 80, 3)):
        process_snapshots(state, [step], FakeModel(), baseline_df)
    assert "not flagged: z=" in caplog.text

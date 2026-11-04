import math

import numpy as np
import pandas as pd
import pytest

from app.nba.moments import analyze_game, scoring_events


class MarginModel:
    """P(home wins) rises with the margin, and more steeply late in the game."""

    def __init__(self):
        self.calls = 0

    def predict_proba(self, X):
        self.calls += 1
        margin = X["margin"].to_numpy(dtype=float)
        remaining = X["time_remaining_seconds"].to_numpy(dtype=float)
        scale = 0.08 + 0.6 * (1 - remaining / 2880.0) ** 4 * 5
        p = 1 / (1 + np.exp(-margin * scale))
        return np.column_stack([1 - p, p])


@pytest.fixture
def baseline():
    # An 8-12 point run usually takes ~4.5 minutes, std 0.5 in log-seconds.
    rows = []
    for magnitude in range(6, 16):
        for period in (1, 2, 3, 4):
            for bucket in (0, 300, 600):
                rows.append(
                    {
                        "period": period,
                        "time_bucket": bucket,
                        "magnitude": magnitude,
                        "mean_log_duration": math.log(270.0),
                        "std_log_duration": 0.5,
                        "n": 60,
                    }
                )
    return pd.DataFrame(rows)


def clock(seconds):
    return f"PT{int(seconds // 60):02d}M{seconds % 60:05.2f}S"


def action(period, seconds, home, away, desc="", team="BOS"):
    return {
        "period": period,
        "clock": clock(seconds),
        "teamTricode": team,
        "description": desc,
        "scoreHome": "" if home is None else str(home),
        "scoreAway": "" if away is None else str(away),
    }


def feed(actions):
    return {"game": {"actions": actions}}


def alternating_feed(n_pairs, period=1, start=700, step=20):
    """A tight back-and-forth game: home 2, away 2, home 2, ..."""
    actions, home, away, t = [], 0, 0, start
    for _ in range(n_pairs):
        home += 2
        actions.append(action(period, t, home, away, "home layup"))
        t -= step
        away += 2
        actions.append(action(period, t, home, away, "away layup", team="NYK"))
        t -= step
    return actions, home, away


def test_non_scoring_and_bad_score_actions_are_ignored():
    actions = [
        action(1, 720, 0, 0, "Jump ball"),
        action(1, 700, 2, 0, "layup"),
        action(1, 690, None, None, "MISS jumper"),  # empty strings
        {"period": 1, "clock": clock(680), "scoreHome": "abc", "scoreAway": "0"},  # junk
        action(1, 670, 2, 0, "rebound"),  # unchanged score
        action(1, 660, 2, 3, "three", team="NYK"),
        action(0, 650, 4, 3, "bad period"),
        "not even a dict",
    ]
    events = scoring_events(feed(actions))
    assert [(e.side, e.points, e.home_score, e.away_score) for e in events] == [
        ("home", 2, 2, 0),
        ("away", 3, 2, 3),
    ]
    assert events[0].t == pytest.approx(20.0)


def test_short_feed_returns_nothing(baseline):
    actions, _, _ = alternating_feed(4)  # 8 scoring events < 10
    model = MarginModel()
    result = analyze_game(feed(actions), model, baseline, "BOS", "NYK", min_run_z=2.0)
    assert result["swings"] == [] and result["runs"] == []
    assert result["lead_changes"] == 0
    assert model.calls == 0


def test_go_ahead_clutch_three_is_the_top_swing(baseline):
    actions, home, away = alternating_feed(10)
    # Q4: home leads by 2 late, then the away team hits a three with 8s left.
    actions.append(action(4, 40, home + 2, away, "home jumper"))
    actions.append(action(4, 8, home + 2, away + 3, "J. Doe 26' 3PT Jump Shot (3 PTS)", team="NYK"))
    model = MarginModel()
    result = analyze_game(feed(actions), model, baseline, "BOS", "NYK", min_run_z=2.0)

    assert model.calls == 1  # one batched predict_proba for the whole game
    top = result["swings"][0]
    assert top["team"] == "NYK"
    assert top["detail"] == "J. Doe 26' 3PT Jump Shot (3 PTS)"
    assert top["when"] == "Q4 0:08"
    assert top["tags"] == ["Go-ahead", "Clutch"]
    assert top["wp_before"] < 0.5 < top["wp_after"]
    assert top["swing"] == pytest.approx(top["wp_after"] - top["wp_before"], abs=1e-3)
    assert top["score"] == f"NYK {away + 3}, BOS {home + 2}"
    assert len(result["swings"]) <= 3


def test_tying_basket_is_tagged(baseline):
    actions, home, away = alternating_feed(10)
    actions.append(action(4, 30, home + 3, away, "home three"))
    actions.append(action(4, 5, home + 3, away + 3, "tying three", team="NYK"))
    result = analyze_game(feed(actions), MarginModel(), baseline, "BOS", "NYK", min_run_z=2.0)
    tying = next(s for s in result["swings"] if s["detail"] == "tying three")
    assert "Ties the game" in tying["tags"]
    assert "Go-ahead" not in tying["tags"]


def _run_feed(run_step_seconds):
    actions, home, away = alternating_feed(6, period=2, start=700, step=15)
    t = 500
    for _ in range(5):  # home scores 10 straight
        home += 2
        actions.append(action(2, t, home, away, "home bucket"))
        t -= run_step_seconds
    away += 2
    actions.append(action(2, t, home, away, "away answers", team="NYK"))
    return actions


def test_fast_run_flags_and_slow_run_does_not(baseline):
    # 10 points in 40 s: z = (log 270 - log 40) / 0.5 = 3.8
    fast = analyze_game(feed(_run_feed(10)), MarginModel(), baseline, "BOS", "NYK", min_run_z=2.0)
    assert len(fast["runs"]) == 1
    run = fast["runs"][0]
    assert run["team"] == "BOS"
    assert run["points"] == 10
    assert run["duration_sec"] == pytest.approx(40.0)
    assert run["z_score"] == pytest.approx((math.log(270) - math.log(40)) / 0.5, abs=0.01)
    assert run["when"] == "Q2 8:20"

    # 10 points in 4 minutes: z < 1, not shown
    slow = analyze_game(feed(_run_feed(60)), MarginModel(), baseline, "BOS", "NYK", min_run_z=2.0)
    assert slow["runs"] == []


def test_lead_changes_and_largest_lead(baseline):
    scores = [(2, 0), (2, 3), (4, 3), (4, 5), (4, 7), (6, 7), (8, 7), (10, 7), (12, 7), (14, 7), (14, 9), (16, 9)]
    actions = [action(1, 700 - 20 * i, h, a, "score") for i, (h, a) in enumerate(scores)]
    result = analyze_game(feed(actions), MarginModel(), baseline, "BOS", "NYK", min_run_z=2.0)
    # BOS -> NYK -> BOS -> NYK -> BOS
    assert result["lead_changes"] == 4
    assert result["largest_lead"] == 7
    assert result["largest_lead_team"] == "BOS"

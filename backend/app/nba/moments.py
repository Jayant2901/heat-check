"""Finds a finished game's notable moments from its play-by-play.

A finished game's play-by-play gives every scoring play with its exact clock
(the live engine now reads the same play-by-play as it is written), so:

- **swings** are the single plays that moved win probability the most. Each
  play's "before" and "after" probabilities are computed at the same clock
  with the pre- and post-play margin, so a swing measures the play itself,
  not the time that passed since the previous basket.
- **runs** are unanswered scoring runs (same RunDetector and 120 s gap rule
  the baseline was built with), z-scored with the existing z_score_run.
  Like build_baseline.py, a run is bucketed by where it STARTED. The
  baseline has very few runs of 18+ points (some buckets have n=1 and a
  standard deviation of 0), which makes their z-scores meaningless, so a
  long run is scored on its longest opening stretch whose size still has
  at least RUN_MIN_HISTORY historical runs behind it. The card still shows
  the whole run.
- **flow** is lead changes and the largest lead.

All win probabilities for a game come from one batched predict_proba call.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.anomaly.baseline import z_score_run
from app.anomaly.run_detector import DEFAULT_GAP_SECONDS, RunDetector
from app.core.game_state import RunState
from app.model.features import FEATURE_COLUMNS, build_feature_row, elapsed_seconds, time_remaining_seconds
from app.nba.parse import clock_text, parse_clock_seconds

MIN_SCORING_EVENTS = 10
SWING_MIN = 0.08
SWINGS_PER_GAME = 3
RUN_MIN_POINTS = 8
RUNS_PER_GAME = 2
RUN_MIN_HISTORY = 50
CLUTCH_SECONDS = 300
CLUTCH_MARGIN = 5


@dataclass
class ScoringEvent:
    period: int
    clock: float
    t: float
    side: str  # "home" | "away"
    points: int
    home_score: int
    away_score: int
    description: str


def _score_value(raw) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def scoring_events(pbp: dict) -> list[ScoringEvent]:
    """Every action where exactly one team's score went up.

    scoreHome/scoreAway are strings and may be empty or junk on non-scoring
    actions; those are ignored, as are score corrections (a score going
    down) and the rare action where both scores move at once.
    """
    actions = ((pbp or {}).get("game") or {}).get("actions") or []
    home = away = 0
    events = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        new_home = _score_value(action.get("scoreHome"))
        new_away = _score_value(action.get("scoreAway"))
        if new_home is None or new_away is None:
            continue
        home_delta, away_delta = new_home - home, new_away - away
        home, away = new_home, new_away
        if (home_delta > 0) == (away_delta > 0) or home_delta < 0 or away_delta < 0:
            continue
        try:
            period = int(action.get("period"))
        except (TypeError, ValueError):
            continue
        if period <= 0:
            continue
        clock = parse_clock_seconds(action.get("clock"))
        side, points = ("home", home_delta) if home_delta > 0 else ("away", away_delta)
        events.append(
            ScoringEvent(
                period=period,
                clock=clock,
                t=elapsed_seconds(period, clock),
                side=side,
                points=points,
                home_score=home,
                away_score=away,
                description=(action.get("description") or "").strip(),
            )
        )
    return events


def _batch_home_wp(model, rows: list[dict]) -> np.ndarray:
    X = pd.DataFrame(rows, columns=FEATURE_COLUMNS)
    return np.asarray(model.predict_proba(X), dtype=float)[:, 1]


def _score_text(home_code: str, away_code: str, home: int, away: int) -> str:
    return f"{away_code} {away}, {home_code} {home}"


def _swings(events, wp_before, wp_after, home_code, away_code) -> list[dict]:
    swings = []
    for event, before, after in zip(events, wp_before, wp_after):
        sign = 1 if event.side == "home" else -1
        swing = (after - before) * sign
        if swing < SWING_MIN:
            continue
        margin_after = (event.home_score - event.away_score) * sign  # beneficiary's view
        margin_before = margin_after - event.points
        tags = []
        if margin_before <= 0 < margin_after:
            tags.append("Go-ahead")
        if margin_after == 0:
            tags.append("Ties the game")
        if event.period >= 4 and event.clock <= CLUTCH_SECONDS and abs(margin_after) <= CLUTCH_MARGIN:
            tags.append("Clutch")
        team_before = before if sign == 1 else 1 - before
        team_after = after if sign == 1 else 1 - after
        swings.append(
            {
                "team": home_code if sign == 1 else away_code,
                "side": event.side,
                "swing": round(float(swing), 4),
                "wp_before": round(float(team_before), 4),
                "wp_after": round(float(team_after), 4),
                "when": clock_text(event.period, event.clock),
                "t": event.t,
                "score": _score_text(home_code, away_code, event.home_score, event.away_score),
                "home_score": event.home_score,
                "away_score": event.away_score,
                "detail": event.description,
                "tags": tags,
            }
        )
    swings.sort(key=lambda s: s["swing"], reverse=True)
    return swings[:SWINGS_PER_GAME]


def _largest_supported_magnitude(baseline) -> int:
    """Largest run size with at least RUN_MIN_HISTORY runs in the baseline."""
    if baseline is None or len(baseline) == 0:
        return 0
    totals = baseline.groupby("magnitude")["n"].sum()
    supported = [int(m) for m, n in totals.items() if n >= RUN_MIN_HISTORY]
    return max(supported, default=0)


def _runs(events, baseline, min_z, home_code, away_code) -> list[dict]:
    detector = RunDetector(gap_seconds=DEFAULT_GAP_SECONDS)
    cap = _largest_supported_magnitude(baseline)
    current: RunState | None = None
    run_events: list[ScoringEvent] = []
    candidates = []

    def consider(run: RunState | None, plays: list[ScoringEvent]) -> None:
        if run is None or not plays or run.points_scored < RUN_MIN_POINTS:
            return
        first, end = plays[0], plays[-1]
        # Score the longest opening stretch the baseline can actually judge.
        basis_points, basis_end, total = 0, None, 0
        for play in plays:
            total += play.points
            if total <= cap:
                basis_points, basis_end = total, play
        if basis_end is None or basis_points < RUN_MIN_POINTS:
            return
        basis = RunState(
            team=run.team,
            start_elapsed_seconds=first.t,
            start_score_diff=0,
            points_scored=basis_points,
            last_scored_elapsed=basis_end.t,
        )
        result = z_score_run(baseline, basis, basis_end.t, first.clock, first.period)
        if result is None:
            return
        z, bucket = result
        if not math.isfinite(z) or z < min_z:
            return
        duration = run.last_scored_elapsed - run.start_elapsed_seconds
        candidates.append(
            {
                "team": home_code if run.team == "home" else away_code,
                "side": run.team,
                "points": run.points_scored,
                "duration_sec": round(duration, 1),
                "z_score": round(float(z), 2),
                "z_basis_points": basis_points,
                "bucket": bucket,
                "when": clock_text(first.period, first.clock),
                "ends": clock_text(end.period, end.clock),
                "t": run.start_elapsed_seconds,
                "score_after": _score_text(home_code, away_code, end.home_score, end.away_score),
                "home_score": end.home_score,
                "away_score": end.away_score,
            }
        )

    for event in events:
        update = detector.on_score(current, event.side, event.points, event.t, event.home_score - event.away_score)
        if update.closed_run is not None:
            consider(update.closed_run, run_events)
        if update.current_run is not current:
            run_events = []
        run_events.append(event)
        current = update.current_run
    consider(current, run_events)

    candidates.sort(key=lambda r: r["z_score"], reverse=True)
    return candidates[:RUNS_PER_GAME]


def _flow(events, home_code, away_code) -> dict:
    lead_changes = 0
    leader = None
    largest = 0
    largest_team = None
    for event in events:
        margin = event.home_score - event.away_score
        if margin != 0:
            now_leader = "home" if margin > 0 else "away"
            if leader is not None and now_leader != leader:
                lead_changes += 1
            leader = now_leader
        if abs(margin) > largest:
            largest = abs(margin)
            largest_team = home_code if margin > 0 else away_code
    return {"lead_changes": lead_changes, "largest_lead": largest, "largest_lead_team": largest_team}


def empty_moments() -> dict:
    return {
        "swings": [],
        "runs": [],
        "lead_changes": 0,
        "largest_lead": 0,
        "largest_lead_team": None,
        "n_scoring_events": 0,
    }


def analyze_game(pbp: dict, model, baseline, home_code: str, away_code: str, min_run_z: float) -> dict:
    events = scoring_events(pbp)
    if len(events) < MIN_SCORING_EVENTS:
        result = empty_moments()
        result["n_scoring_events"] = len(events)
        return result

    rows = []
    for event in events:
        remaining = time_remaining_seconds(event.period, event.clock)
        margin_after = event.home_score - event.away_score
        signed_points = event.points if event.side == "home" else -event.points
        rows.append(build_feature_row(margin_after - signed_points, remaining, event.period))
        rows.append(build_feature_row(margin_after, remaining, event.period))
    wp = _batch_home_wp(model, rows)
    wp_before, wp_after = wp[0::2], wp[1::2]

    return {
        "swings": _swings(events, wp_before, wp_after, home_code, away_code),
        "runs": _runs(events, baseline, min_run_z, home_code, away_code),
        **_flow(events, home_code, away_code),
        "n_scoring_events": len(events),
    }

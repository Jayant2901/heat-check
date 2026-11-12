"""The shared step: snapshots -> state update -> WP -> run/anomaly -> events.

Both DataSource implementations (datasources/nba_live.py,
datasources/replay_source.py) produce the same GameSnapshot shape, and this
module is the ONLY place a snapshot turns into WP/anomaly/SSE events -- so
live and replay run the same code.

What a snapshot means decides how exact the run detection is
(GameSnapshot.exact, core/interfaces.py):

* play-by-play (exact=True): one snapshot per action, so a score change is one
  scoring play with its true clock. Runs are measured the way the baseline was
  built, and the result does not depend on how many plays arrive per poll or
  on when a viewer connected. This is the path for live games (when the feed
  has play-by-play), for curated replays and for past-game replays.
* boxscore ticks (exact=False): several plays may fall between two polls. If
  both teams scored in the window their order is unknown, so the run in
  progress is dropped, and a run is timed from the poll that first saw it.
  Lower fidelity; used only when play-by-play is unavailable, and labelled
  "boxscore_ticks" in every event.
  test_engine.py::test_boxscore_ticks_depend_on_sampling pins that gap.

The win-probability model and anomaly baseline are passed in explicitly
rather than imported as globals, so tests can inject trivial fakes without
a real trained artifact on disk. A model's predict_proba must return one row
per input row.
"""

import logging
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from app.anomaly.baseline import z_score_run
from app.anomaly.run_detector import RunDetector
from app.core.config import settings
from app.core.events import AnomalyEvent as AnomalyEventOut
from app.core.events import GameEndEvent, SSEEvent, WPUpdateEvent
from app.core.game_state import AnomalyEvent, GameState, RunState, ScoreHistoryPoint
from app.core.interfaces import FIDELITY_BOXSCORE_TICKS, FIDELITY_PLAY_BY_PLAY, GameSnapshot
from app.model.features import FEATURE_COLUMNS, build_feature_row, elapsed_seconds, time_remaining_seconds
from app.model.win_prob import WinProbModel

logger = logging.getLogger(__name__)

_run_detector = RunDetector()


_RARITY_PHRASES = {
    # Below z=2.5 a run is quicker than usual, not rare: the 1.5 threshold
    # was tuned on 3 games (see core/config.py), so the copy must not oversell.
    "mild": (
        "quicker than usual for this point in the game",
        "a faster burst than this stage of a game usually sees",
        "a quicker scoring stretch than usual here",
    ),
    "extreme": (
        "about as fast as scoring runs get at this stage of a game",
        "one of the most extreme streaks in the data",
        "a streak that stands out even among rare ones",
    ),
    "high": (
        "the kind of run that only shows up a handful of times a season",
        "far outside the normal pace of scoring here",
        "an extremely rare scoring streak for this point in the game",
    ),
    "mid": (
        "not something you see often at this stage of a game",
        "well outside the usual pace for this point in the game",
        "an unusually quick scoring streak for this point in the game",
    ),
}


def _rarity_tier(z: float) -> str:
    if z >= 3.5:
        return "extreme"
    if z >= 3.0:
        return "high"
    if z >= 2.5:
        return "mid"
    return "mild"


def _describe_rarity(z: float, seed: int) -> tuple[str, str]:
    """Plain-language rarity phrase for a z-score, for the layman-facing message.

    Thresholds are qualitative, not derived from an exact percentile lookup,
    since the baseline table only gives us mean/std per bucket, not a full
    distribution to invert. `seed` picks among a few phrasings per tier so
    consecutive heat checks don't all read as the same copy-pasted line --
    it's derived from the run itself, so a given run always describes itself
    the same way.
    """
    tier = _rarity_tier(z)
    phrases = _RARITY_PHRASES[tier]
    return tier, phrases[seed % len(phrases)]


def _normalise(snapshot: GameSnapshot) -> tuple[int, float]:
    period, clock = snapshot.period, snapshot.game_clock_seconds_remaining
    if period <= 0:
        # Pre-game boxscores report period 0 (and often an empty clock);
        # treat that as the opening tip, Q1 12:00, instead of producing a
        # negative elapsed time.
        return 1, 720.0
    return period, clock


def _home_win_probs(model: WinProbModel, snapshots: list[GameSnapshot]) -> np.ndarray:
    """P(home wins) for every snapshot in one model call."""
    rows = []
    for snap in snapshots:
        period, clock = _normalise(snap)
        rows.append(build_feature_row(snap.home_score - snap.away_score, time_remaining_seconds(period, clock), period))
    X = pd.DataFrame(rows, columns=FEATURE_COLUMNS)
    probs = np.asarray(model.predict_proba(X), dtype=float)
    if probs.shape[0] != len(rows):
        raise ValueError("predict_proba must return one row per input row")
    return probs[:, 1]


def run_payload(run: RunState | None, t: float, exact: bool) -> dict | None:
    """The scoring run in progress, as sent on every wp_update (or None).

    A run counts as in progress until the detector's gap with no scoring
    runs out. Duration is first basket to latest basket for play-by-play
    (what the baseline measures); for boxscore ticks it is time since the
    poll that first saw the run, the best a poll can know.
    """
    if run is None or not _run_detector.is_active(run, t):
        return None
    end = run.last_scored_elapsed if exact else t
    return {
        "team": run.team,
        "points": run.points_scored,
        "duration_sec": round(max(0.0, end - run.start_elapsed_seconds), 1),
        "since_last_score_sec": round(max(0.0, t - run.last_scored_elapsed), 1),
        "z_score": None if run.z_score is None else round(run.z_score, 2),
        "threshold": settings.anomaly_z_threshold,
        "min_points": settings.anomaly_min_magnitude,
        "flagged": run.anomaly_flagged,
        "started_t": run.start_elapsed_seconds,
    }


def _score_and_flag(state: GameState, t: float, period: int, clock: float, exact: bool, baseline, scored_now: bool):
    """Z-score the current run and, the first time it clears the threshold,
    return the heat-check as (state record, message pieces); else None.

    Every reason a candidate is not flagged is logged at debug level.
    """
    run = state.current_run
    if run is None:
        return None
    if exact and not scored_now:
        return None  # nothing about the run changed since it was last scored
    if run.points_scored < settings.anomaly_min_magnitude:
        logger.debug("%s: run of %d by %s below minimum %d", state.game_id, run.points_scored, run.team, settings.anomaly_min_magnitude)
        return None
    end = t if not exact else run.last_scored_elapsed
    # Bucketed by where the run started, like the baseline.
    start_period = run.start_period or period
    start_clock = run.start_clock if run.start_period else clock
    result = z_score_run(baseline, run, end, start_clock, start_period)
    if result is None:
        run.z_score = None
        logger.debug("%s: run of %d by %s could not be scored (see baseline log)", state.game_id, run.points_scored, run.team)
        return None
    z, bucket_label = float(result[0]), result[1]
    run.z_score = z  # kept fresh even after the flag, so the live meter keeps climbing
    if run.anomaly_flagged:
        return None
    # Only a POSITIVE z is a "heat check" moment (run happened faster than
    # history says it should -- see anomaly/baseline.py). A negative z just
    # means the run took longer than usual, which is unremarkable.
    if z < settings.anomaly_z_threshold:
        logger.debug(
            "%s: run of %d by %s not flagged: z=%.2f below threshold %.2f",
            state.game_id, run.points_scored, run.team, z, settings.anomaly_z_threshold,
        )
        return None
    return z, bucket_label, end - run.start_elapsed_seconds


def process_snapshots(
    state: GameState,
    snapshots: list[GameSnapshot],
    win_prob_model: WinProbModel,
    baseline,
    *,
    live: bool = False,
    now: float | None = None,
) -> list[SSEEvent]:
    """Apply `snapshots` (oldest first) to `state`; return the events to send.

    `live=True` marks a poll of a game in progress (not a backfill of
    history or a replay), which is the only time feed delay is reported.
    """
    if not snapshots:
        return []
    now = time.time() if now is None else now
    wp_values = _home_win_probs(win_prob_model, snapshots)
    events: list[SSEEvent] = []

    def emit(event):
        state.seq += 1
        event = replace(event, seq=state.seq)
        state.log.append(event)
        events.append(event)

    for snapshot, wp_home in zip(snapshots, wp_values):
        number = snapshot.action_number
        if number is not None and state.last_action_number is not None and number <= state.last_action_number:
            continue  # this play was already applied
        if number is not None:
            state.last_action_number = number

        period, clock = _normalise(snapshot)
        t = elapsed_seconds(period, clock)
        exact = snapshot.exact
        was_final = state.status == "final"

        # The first snapshot of a game we joined has no earlier score to
        # compare with: how the score got there is unknown, so no run.
        has_history = bool(state.score_history)
        home_delta = snapshot.home_score - state.home_score if has_history else 0
        away_delta = snapshot.away_score - state.away_score if has_history else 0

        state.period = period
        state.clock_seconds_remaining = clock
        state.home_team = snapshot.home_team
        state.away_team = snapshot.away_team
        state.home_score = snapshot.home_score
        state.away_score = snapshot.away_score
        state.status = snapshot.game_status
        state.fidelity = FIDELITY_PLAY_BY_PLAY if exact else FIDELITY_BOXSCORE_TICKS

        scored_now = False
        if home_delta > 0 and away_delta > 0:
            # Both teams' scores moved at once. From boxscore polls this is
            # ordinary (the window held several plays) and their order is
            # unknowable, so the run is dropped rather than guessed. From
            # play-by-play it means a play was missing from the feed, which is
            # rare and equally unorderable.
            state.current_run = _run_detector.reset(state.current_run)
        elif home_delta > 0 or away_delta > 0:
            team, points = ("home", home_delta) if home_delta > 0 else ("away", away_delta)
            margin_after = state.home_score - state.away_score
            update = _run_detector.on_score(state.current_run, team, points, t, margin_after, period, clock)
            state.current_run = update.current_run
            scored_now = True

        flag = _score_and_flag(state, t, period, clock, exact, baseline, scored_now)
        run = state.current_run
        if flag is not None:
            run.anomaly_flagged = True

        state.score_history.append(
            ScoreHistoryPoint(elapsed_seconds=t, home_score=state.home_score, away_score=state.away_score, wp_home=float(wp_home))
        )
        feed_delay = None
        if live and snapshot.source_ts is not None:
            feed_delay = round(max(0.0, now - snapshot.source_ts), 2)
        emit(
            WPUpdateEvent(
                type="wp_update",
                t=t,
                period=period,
                clock=clock,
                home_team=state.home_team,
                away_team=state.away_team,
                home_score=state.home_score,
                away_score=state.away_score,
                wp_home=float(wp_home),
                wp_away=1.0 - float(wp_home),
                status=snapshot.game_status,
                run=run_payload(run, t, exact),
                fidelity=state.fidelity,
                source_ts=snapshot.source_ts,
                server_ts=now,
                feed_delay_sec=feed_delay,
            )
        )

        if flag is not None:
            z, bucket_label, duration = flag
            seed = run.points_scored * 31 + round(duration)
            tier, rarity = _describe_rarity(z, seed)
            # run.team is "home"/"away"; the message reads better with the
            # real team code. The event's `team` field stays "home"/"away" --
            # the frontend keys colours off it.
            team_code = (state.home_team if run.team == "home" else state.away_team) or run.team
            message = f"{team_code} just scored {run.points_scored} unanswered points in {duration:.0f} seconds — {rarity}."
            state.anomalies.append(
                AnomalyEvent(
                    elapsed_seconds=t,
                    team=run.team,
                    magnitude=run.points_scored,
                    duration_seconds=duration,
                    z_score=z,
                    bucket=bucket_label,
                    message=message,
                )
            )
            emit(
                AnomalyEventOut(
                    type="anomaly",
                    t=t,
                    team=run.team,
                    magnitude=run.points_scored,
                    duration_sec=duration,
                    z_score=z,
                    bucket=bucket_label,
                    message=message,
                    period=period,
                    clock=clock,
                    team_code=team_code,
                    tier=tier,
                    source_ts=snapshot.source_ts,
                    server_ts=now,
                )
            )

        if snapshot.game_status == "final" and not was_final:
            winner = state.home_team if state.home_score > state.away_score else state.away_team
            emit(
                GameEndEvent(
                    type="game_end",
                    final_home_score=state.home_score,
                    final_away_score=state.away_score,
                    winner=winner,
                )
            )

    return events


def process_tick(
    state: GameState,
    snapshot: GameSnapshot,
    win_prob_model: WinProbModel,
    baseline,
    **kwargs,
) -> list[SSEEvent]:
    """One snapshot; see process_snapshots."""
    return process_snapshots(state, [snapshot], win_prob_model, baseline, **kwargs)

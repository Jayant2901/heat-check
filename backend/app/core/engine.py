"""The shared tick: snapshot -> state update -> WP -> anomaly -> events.

Both DataSource implementations (datasources/nba_live.py,
datasources/replay_source.py) produce identical GameSnapshot objects; this
module is the ONLY place a snapshot turns into WP/anomaly/SSE events, which
is what guarantees live and replay behave identically -- enforced by
backend/tests/test_replay_vs_live_equivalence.py.

The win-probability model and anomaly baseline are passed in explicitly
rather than imported as globals, so tests can inject trivial fakes without
a real trained artifact on disk.
"""

from app.anomaly.baseline import z_score_run
from app.anomaly.run_detector import RunDetector
from app.core.config import settings
from app.core.events import AnomalyEvent as AnomalyEventOut
from app.core.events import GameEndEvent, SSEEvent, WPUpdateEvent
from app.core.game_state import AnomalyEvent, GameState, ScoreHistoryPoint
from app.core.interfaces import GameSnapshot
from app.model.features import elapsed_seconds, time_remaining_seconds
from app.model.win_prob import WinProbModel, predict_win_prob

_run_detector = RunDetector()


def process_tick(
    state: GameState,
    snapshot: GameSnapshot,
    win_prob_model: WinProbModel,
    baseline,
) -> list[SSEEvent]:
    events: list[SSEEvent] = []
    t = elapsed_seconds(snapshot.period, snapshot.game_clock_seconds_remaining)

    home_delta = snapshot.home_score - state.home_score
    away_delta = snapshot.away_score - state.away_score

    state.period = snapshot.period
    state.clock_seconds_remaining = snapshot.game_clock_seconds_remaining
    state.home_team = snapshot.home_team
    state.away_team = snapshot.away_team
    state.home_score = snapshot.home_score
    state.away_score = snapshot.away_score
    state.status = snapshot.game_status

    if home_delta > 0 and away_delta > 0:
        # Both teams scored between polls -- the ~10-15s poll interval can't
        # disambiguate the order, so treat this tick as run-neutral rather
        # than guess. See run_detector.py's docstring.
        state.current_run = _run_detector.reset(state.current_run)
    elif home_delta > 0 or away_delta > 0:
        team, points = ("home", home_delta) if home_delta > 0 else ("away", away_delta)
        margin_after = state.home_score - state.away_score
        update = _run_detector.on_score(state.current_run, team, points, t, margin_after)
        state.current_run = update.current_run

    margin = state.home_score - state.away_score
    time_remaining = time_remaining_seconds(snapshot.period, snapshot.game_clock_seconds_remaining)
    wp_home = predict_win_prob(win_prob_model, margin, time_remaining, snapshot.period)
    wp_away = 1.0 - wp_home

    state.score_history.append(
        ScoreHistoryPoint(elapsed_seconds=t, home_score=state.home_score, away_score=state.away_score, wp_home=wp_home)
    )
    events.append(
        WPUpdateEvent(
            type="wp_update",
            t=t,
            period=snapshot.period,
            clock=snapshot.game_clock_seconds_remaining,
            home_team=state.home_team,
            away_team=state.away_team,
            home_score=state.home_score,
            away_score=state.away_score,
            wp_home=wp_home,
            wp_away=wp_away,
        )
    )

    if (
        state.current_run is not None
        and not state.current_run.anomaly_flagged
        and state.current_run.points_scored >= settings.anomaly_min_magnitude
    ):
        result = z_score_run(baseline, state.current_run, t, snapshot.game_clock_seconds_remaining, snapshot.period)
        if result is not None:
            z, bucket_label = result
            # Only a POSITIVE z is a "heat check" moment (run happened faster
            # than history says it should -- see anomaly/baseline.py). A
            # negative z just means the run took longer than usual, which is
            # unremarkable, not anomalous, and shouldn't be flagged.
            if z >= settings.anomaly_z_threshold:
                state.current_run.anomaly_flagged = True
                duration = t - state.current_run.start_elapsed_seconds
                message = (
                    f"{state.current_run.team} {state.current_run.points_scored}-0 run in "
                    f"{duration:.0f}s is a {z:.1f}-sigma outlier for this point in the game"
                )
                state.anomalies.append(
                    AnomalyEvent(
                        elapsed_seconds=t,
                        team=state.current_run.team,
                        magnitude=state.current_run.points_scored,
                        duration_seconds=duration,
                        z_score=z,
                        bucket=bucket_label,
                        message=message,
                    )
                )
                events.append(
                    AnomalyEventOut(
                        type="anomaly",
                        t=t,
                        team=state.current_run.team,
                        magnitude=state.current_run.points_scored,
                        duration_sec=duration,
                        z_score=z,
                        bucket=bucket_label,
                        message=message,
                    )
                )

    if snapshot.game_status == "final":
        winner = state.home_team if state.home_score > state.away_score else state.away_team
        events.append(
            GameEndEvent(
                type="game_end",
                final_home_score=state.home_score,
                final_away_score=state.away_score,
                winner=winner,
            )
        )

    return events

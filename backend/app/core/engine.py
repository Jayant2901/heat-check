"""The shared tick: snapshot -> state update -> WP -> anomaly -> events.

Both DataSource implementations (datasources/nba_live.py,
datasources/replay_source.py) produce the same GameSnapshot shape, and this
module is the ONLY place a snapshot turns into WP/anomaly/SSE events -- so
live and replay run the same code.

They are NOT equivalent in what they see, though. Replay fixtures tick every
~3 s of game time; live polling sees one boxscore every 12 s of wall-clock
time. Run detection works on score deltas between ticks, so coarser
sampling merges scoring plays, changes measured run durations and can turn a
flag on or off. test_engine.py::test_sampling_rate_changes_what_gets_flagged
pins that gap on a real fixture (0 flags at stride 1, 1 at stride 4).

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


def process_tick(
    state: GameState,
    snapshot: GameSnapshot,
    win_prob_model: WinProbModel,
    baseline,
) -> list[SSEEvent]:
    events: list[SSEEvent] = []
    period, clock = snapshot.period, snapshot.game_clock_seconds_remaining
    if period <= 0:
        # Pre-game boxscores report period 0 (and often an empty clock);
        # treat that as the opening tip, Q1 12:00, instead of producing a
        # negative elapsed time.
        period, clock = 1, 720.0
    t = elapsed_seconds(period, clock)

    home_delta = snapshot.home_score - state.home_score
    away_delta = snapshot.away_score - state.away_score

    state.period = period
    state.clock_seconds_remaining = clock
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
    time_remaining = time_remaining_seconds(period, clock)
    wp_home = predict_win_prob(win_prob_model, margin, time_remaining, period)
    wp_away = 1.0 - wp_home

    state.score_history.append(
        ScoreHistoryPoint(elapsed_seconds=t, home_score=state.home_score, away_score=state.away_score, wp_home=wp_home)
    )
    events.append(
        WPUpdateEvent(
            type="wp_update",
            t=t,
            period=period,
            clock=clock,
            home_team=state.home_team,
            away_team=state.away_team,
            home_score=state.home_score,
            away_score=state.away_score,
            wp_home=wp_home,
            wp_away=wp_away,
            status=snapshot.game_status,
        )
    )

    if (
        state.current_run is not None
        and not state.current_run.anomaly_flagged
        and state.current_run.points_scored >= settings.anomaly_min_magnitude
    ):
        result = z_score_run(baseline, state.current_run, t, clock, period)
        if result is not None:
            z, bucket_label = result
            # Only a POSITIVE z is a "heat check" moment (run happened faster
            # than history says it should -- see anomaly/baseline.py). A
            # negative z just means the run took longer than usual, which is
            # unremarkable, not anomalous, and shouldn't be flagged.
            if z >= settings.anomaly_z_threshold:
                state.current_run.anomaly_flagged = True
                duration = t - state.current_run.start_elapsed_seconds
                seed = state.current_run.points_scored * 31 + round(duration)
                _tier, rarity = _describe_rarity(z, seed)
                # current_run.team is "home"/"away"; the message reads
                # better with the real team code. The event's `team` field
                # stays "home"/"away" -- the frontend keys colours off it.
                team_code = state.home_team if state.current_run.team == "home" else state.away_team
                message = (
                    f"{team_code or state.current_run.team} just scored {state.current_run.points_scored} unanswered "
                    f"points in {duration:.0f} seconds — {rarity}."
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

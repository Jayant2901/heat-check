import json

from fastapi.testclient import TestClient

from app.api.stream import _already_in_backlog, _backlog_events
from app.core.events import DegradedEvent, GameEndEvent, WPUpdateEvent
from app.core.game_state import AnomalyEvent, GameState, ScoreHistoryPoint
from app.main import app
from app.polling.poller import poller_manager

client = TestClient(app)


def test_backlog_events_replays_score_history_then_anomalies():
    state = GameState(game_id="T1")
    state.score_history.append(ScoreHistoryPoint(elapsed_seconds=10, home_score=2, away_score=0, wp_home=0.51))
    state.anomalies.append(
        AnomalyEvent(
            elapsed_seconds=90,
            team="home",
            magnitude=8,
            duration_seconds=40,
            z_score=3.0,
            bucket="exact",
            message="home 8-0 run in 40s is a 3.0-sigma outlier for this point in the game",
        )
    )

    events = list(_backlog_events(state))
    assert [e["event"] for e in events] == ["wp_update", "anomaly"]
    assert json.loads(events[0]["data"])["home_score"] == 2
    assert json.loads(events[1]["data"])["z_score"] == 3.0


def _wp_event(t):
    return WPUpdateEvent(
        type="wp_update",
        t=t,
        period=1,
        clock=600,
        home_team="HOM",
        away_team="AWY",
        home_score=2,
        away_score=0,
        wp_home=0.55,
        wp_away=0.45,
    )


def test_already_in_backlog_drops_events_at_or_before_the_cutoff():
    assert _already_in_backlog(_wp_event(5), last_backlog_t=10) is True
    assert _already_in_backlog(_wp_event(10), last_backlog_t=10) is True  # exactly at the cutoff -> covered
    assert _already_in_backlog(_wp_event(15), last_backlog_t=10) is False


def test_already_in_backlog_never_drops_events_without_a_t():
    assert _already_in_backlog(GameEndEvent(type="game_end", final_home_score=1, final_away_score=0, winner="HOM"), 999) is False
    assert _already_in_backlog(DegradedEvent(type="degraded", reason="test"), 999) is False


def test_second_live_game_conflicts_with_an_already_tracked_one():
    # Simulate an already-active live poller without spinning up real
    # network calls -- is_active()/active_keys() only check dict membership.
    poller_manager._tasks["0000000001"] = object()
    try:
        resp = client.get("/api/games/0000000002/stream")
        assert resp.status_code == 409
    finally:
        del poller_manager._tasks["0000000001"]

import asyncio
import json

import pandas as pd
import pytest

from app.core.game_state import game_state_store
from app.datasources.replay_source import ReplaySource
from app.polling.poller import PollerManager
from app.sse.broadcaster import broadcaster

EMPTY_BASELINE = pd.DataFrame(
    [], columns=["period", "time_bucket", "magnitude", "mean_log_duration", "std_log_duration", "n"]
)


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]]


def _snap(period, clock, home, away, status="live"):
    return {
        "game_id": "T1",
        "period": period,
        "game_clock_seconds_remaining": clock,
        "home_team": "HOM",
        "away_team": "AWY",
        "home_score": home,
        "away_score": away,
        "game_status": status,
    }


def _write_fixture(tmp_path, snapshots):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"game_id": "T1", "snapshots": snapshots}))
    return path


@pytest.mark.asyncio
async def test_poller_publishes_events_and_stops_at_game_end(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(1, 700, 2, 0), _snap(4, 0, 100, 90, status="final")]
    fixture_path = _write_fixture(tmp_path, snapshots)

    manager = PollerManager()
    queue = broadcaster.subscribe("T1")
    try:
        manager.start("T1", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 0.01, source_game_id="T1")

        wp_events = [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(3)]
        assert [e.type for e in wp_events] == ["wp_update", "wp_update", "wp_update"]
        assert wp_events[-1].home_score == 100

        game_end = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert game_end.type == "game_end"

        # the task cleans itself up once the source is exhausted
        for _ in range(50):
            if not manager.is_active("T1"):
                break
            await asyncio.sleep(0.01)
        assert not manager.is_active("T1")
        # ...and so does its GameState -- without this, every finished game
        # leaked its full score_history/anomalies forever.
        assert game_state_store.get("T1") is None
    finally:
        broadcaster.unsubscribe("T1", queue)
        game_state_store.remove("T1")


@pytest.mark.asyncio
async def test_starting_an_already_tracked_key_is_a_no_op(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(4, 0, 10, 5, status="final")]
    fixture_path = _write_fixture(tmp_path, snapshots)
    manager = PollerManager()

    try:
        manager.start("T2", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 5.0, source_game_id="T2")
        first_task = manager._tasks["T2"]
        manager.start("T2", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 5.0, source_game_id="T2")

        assert manager._tasks["T2"] is first_task
    finally:
        manager.stop("T2")
        game_state_store.remove("T2")

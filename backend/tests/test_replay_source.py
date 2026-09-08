import json

import pandas as pd
import pytest

from app.core.engine import process_tick
from app.core.game_state import GameState
from app.datasources.replay_source import ReplaySource


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]]


def _write_fixture(tmp_path, snapshots):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"game_id": "T1", "snapshots": snapshots}))
    return path


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


@pytest.mark.asyncio
async def test_replay_source_yields_snapshots_in_order_then_none(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(1, 700, 2, 0), _snap(4, 0, 100, 90, status="final")]
    src = ReplaySource(_write_fixture(tmp_path, snapshots))

    first = await src.next_snapshot("T1")
    second = await src.next_snapshot("T1")
    third = await src.next_snapshot("T1")
    fourth = await src.next_snapshot("T1")

    assert (first.home_score, first.away_score) == (0, 0)
    assert (second.home_score, second.away_score) == (2, 0)
    assert third.game_status == "final"
    assert fourth is None
    assert src.finished


@pytest.mark.asyncio
async def test_replay_drives_the_same_engine_as_live_would(tmp_path):
    """This is the guarantee the whole replay feature depends on: a
    ReplaySource feeds process_tick exactly like NbaLiveDataSource would,
    with no special-casing anywhere downstream."""
    snapshots = [
        _snap(1, 720, 0, 0),
        _snap(1, 700, 2, 0),
        _snap(4, 0, 100, 90, status="final"),
    ]
    src = ReplaySource(_write_fixture(tmp_path, snapshots))
    state = GameState(game_id="T1")
    model = FakeModel()
    baseline = pd.DataFrame([], columns=["period", "time_bucket", "magnitude", "mean_duration", "std_duration", "n"])

    all_events = []
    while not src.finished:
        snapshot = await src.next_snapshot("T1")
        all_events.extend(process_tick(state, snapshot, model, baseline))

    assert len(state.score_history) == 3
    assert state.score_history[-1].home_score == 100
    game_end = [e for e in all_events if e.type == "game_end"]
    assert len(game_end) == 1
    assert game_end[0].winner == "HOM"

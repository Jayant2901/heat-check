import asyncio
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.nba.client as nba_client
import app.nba.replays as replays
import app.nba.weekly as weekly
from app.core.artifacts import get_baseline, get_model
from app.core.game_state import game_state_store
from app.main import app
from app.nba.demo import DemoNbaClient
from app.nba.replays import NotReplayable, snapshots_from_playbyplay
from app.polling.poller import poller_manager

TODAY = date(2026, 11, 4)


def action(period, clock, home, away):
    return {"period": period, "clock": clock, "scoreHome": home, "scoreAway": away, "description": ""}


def test_snapshots_forward_fill_dedupe_and_end_final():
    actions = [action(1, "PT12M00.00S", "0", "0")]
    home = away = 0
    for i in range(1, 13):
        if i % 2:
            home += 2
        else:
            away += 3
        clock = f"PT{11 - i // 2:02d}M{(i % 2) * 30:02d}.00S"
        actions.append(action(1, clock, str(home), str(away)))
        actions.append(action(1, clock, "", ""))  # same moment, no score: adds nothing
    actions.append(action(0, "", "", ""))  # junk, ignored
    snapshots = snapshots_from_playbyplay({"game": {"actions": actions}}, "G1", "BOS", "NYK")
    assert snapshots[0]["home_score"] == 0 and snapshots[0]["game_clock_seconds_remaining"] == 720
    assert len(snapshots) == 13
    assert snapshots[-1]["game_status"] == "final"
    assert all(s["game_status"] == "live" for s in snapshots[:-1])
    assert (snapshots[-1]["home_score"], snapshots[-1]["away_score"]) == (home, away)
    assert {s["home_team"] for s in snapshots} == {"BOS"}


def test_short_feed_is_not_replayable():
    with pytest.raises(NotReplayable):
        snapshots_from_playbyplay({"game": {"actions": [action(1, "PT11M00.00S", "2", "0")]}}, "G", "A", "B")


@pytest.fixture
def demo(monkeypatch):
    client = DemoNbaClient(today=lambda: TODAY)
    monkeypatch.setattr(nba_client, "_client", client)
    weekly.reset_weekly_service()
    service = weekly.WeeklyService(client, get_model(), get_baseline(), today=lambda: TODAY)
    monkeypatch.setattr(weekly, "_service", service)
    replays._cache.clear()
    yield client, service
    weekly.reset_weekly_service()


def _stop_session(key):
    poller_manager.stop(key)
    game_state_store.remove(key)


def test_a_finished_game_from_this_week_can_be_replayed(demo):
    client, service = demo
    asyncio.run(service.build())
    game_id = next(iter(service._finished))
    resp = TestClient(app).post(f"/api/replays/{game_id}/start?speed=50")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_key"].startswith(f"replay:{game_id}:")
    assert body["stream_url"] == f"/api/games/{body['session_key']}/stream"
    assert game_id in replays._cache  # built once, reused
    _stop_session(body["session_key"])


def test_todays_final_game_can_be_replayed_before_the_week_is_built(demo):
    resp = TestClient(app).post("/api/replays/demo-final-1/start")
    assert resp.status_code == 200, resp.text
    _stop_session(resp.json()["session_key"])


def test_unknown_or_unfinished_games_are_404(demo):
    client = TestClient(app)
    assert client.post("/api/replays/nonexistent_game/start").status_code == 404
    # a live game isn't finished, so it can't be replayed yet
    asyncio.run(demo[1].build())
    assert client.post("/api/replays/demo-live-1/start").status_code == 404

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.game_state import game_state_store
from app.main import app
from app.polling.poller import poller_manager

client = TestClient(app)


def test_list_replays_includes_the_committed_fixture():
    resp = client.get("/api/replays")
    assert resp.status_code == 200
    game_ids = [f["game_id"] for f in resp.json()["fixtures"]]
    assert "0022200001" in game_ids


def test_start_replay_missing_fixture_is_404():
    resp = client.post("/api/replays/nonexistent_game/start")
    assert resp.status_code == 404


def test_start_replay_rejects_nonpositive_speed():
    resp = client.post("/api/replays/0022200001/start", params={"speed": 0})
    assert resp.status_code == 422


def test_start_replay_creates_a_streamable_session():
    # Task-lifecycle coverage (does the poller actually run/finish) lives in
    # test_poller.py, which drives PollerManager directly -- Starlette's
    # synchronous TestClient runs each call through a portal that doesn't
    # keep a request-spawned background task alive the way a real running
    # server does, so asserting on it here would be testing the test
    # harness, not the app. This just checks the HTTP contract.
    resp = client.post("/api/replays/0022200001/start", params={"speed": 1000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"].startswith("replay:0022200001:")
    assert body["stream_url"] == f"/api/games/{body['session_key']}/stream"

    poller_manager.stop(body["session_key"])
    game_state_store.remove(body["session_key"])


def test_start_replay_rejects_once_the_concurrent_session_cap_is_hit():
    # Fill up to the cap without spinning up real tasks -- the check only
    # cares about active_keys() dict membership, same trick used for the
    # one-live-game conflict test in test_stream_api.py.
    fake_keys = [f"replay:fake:{i}" for i in range(settings.max_concurrent_replay_sessions)]
    for key in fake_keys:
        poller_manager._tasks[key] = object()
    try:
        resp = client.post("/api/replays/0022200001/start", params={"speed": 1000})
        assert resp.status_code == 429
    finally:
        for key in fake_keys:
            del poller_manager._tasks[key]

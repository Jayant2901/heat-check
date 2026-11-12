from fastapi.testclient import TestClient

import app.core.config as settings_module
from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


class _FakeClient:
    mode = "live"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    async def scoreboard(self):
        if self.error:
            raise self.error
        return self.payload


def test_live_games_lists_todays_ids(monkeypatch):
    payload = {
        "scoreboard": {
            "gameDate": "2026-11-04",
            "games": [
                {"gameId": "0022600101", "gameStatus": 3, "gameTimeUTC": "2026-11-04T23:00:00Z"},
                {"gameId": "0022600102", "gameStatus": 2, "gameTimeUTC": "2026-11-05T00:00:00Z"},
            ],
        }
    }
    monkeypatch.setattr("app.nba.client._client", _FakeClient(payload))
    resp = client.get("/api/games/live")
    assert resp.status_code == 200
    assert resp.json() == {"game_ids": ["0022600102", "0022600101"]}  # live first


def test_live_games_is_an_empty_list_offseason(monkeypatch):
    monkeypatch.setattr("app.nba.client._client", _FakeClient({"scoreboard": {"games": []}}))
    assert client.get("/api/games/live").json() == {"game_ids": []}


def test_scoreboard_is_503_when_nba_is_unreachable(monkeypatch):
    monkeypatch.setattr("app.nba.client._client", _FakeClient(error=RuntimeError("network down")))
    resp = client.get("/api/scoreboard")
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]
    assert "RuntimeError" in resp.json()["detail"]  # says why, not just that it failed


def test_unknown_game_state_is_404():
    resp = client.get("/api/games/nonexistent/state")
    assert resp.status_code == 404


def test_frontend_files_must_be_revalidated():
    for path in ("/", "/src/main.js", "/src/styles.css"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["cache-control"] == "no-cache", path


def test_cors_is_off_unless_origins_are_configured():
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers  # same-origin only by default


def test_cors_allows_only_the_configured_origins(monkeypatch):
    import dataclasses

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    # the same wiring main.py applies when CORS_ALLOW_ORIGINS is set
    origins = ["https://good.example"]
    probe = FastAPI()
    probe.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @probe.get("/x")
    def x():
        return {}

    local = TestClient(probe)
    assert local.get("/x", headers={"Origin": "https://good.example"}).headers["access-control-allow-origin"] == "https://good.example"
    assert "access-control-allow-origin" not in local.get("/x", headers={"Origin": "https://evil.example"}).headers
    assert dataclasses.is_dataclass(settings_module.settings) and settings_module.settings.cors_allow_origins == ()


def test_unhandled_errors_do_not_leak_a_traceback(monkeypatch):
    async def boom():
        raise RuntimeError("secret internal detail /home/user/heat-check")

    monkeypatch.setattr("app.api.games.todays_scoreboard", boom)
    quiet = TestClient(app, raise_server_exceptions=False)
    resp = quiet.get("/api/scoreboard")
    # this route turns scoreboard errors into a readable 503 of its own, so
    # use a route that does not: the lag endpoint with a broken stats call
    monkeypatch.setattr("app.api.games.poller_manager.stats", lambda key: 1 / 0)
    resp = quiet.get("/api/games/x/lag")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}
    assert "Traceback" not in resp.text and "ZeroDivision" not in resp.text


def test_lag_endpoint_for_an_untracked_game_is_404():
    assert client.get("/api/games/nope/lag").status_code == 404


def test_lag_endpoint_reports_the_polls():
    from app.polling.poller import PollStats, _Session, poller_manager

    class S:
        pass

    session = S()
    session.stats = PollStats(started_at=1.0, polls=5, interval_seconds=3.0, last_feed_age_seconds=2.1)
    session.stats.recent_feed_delays = [4.0, 6.0, 5.0]
    session.base_interval = 3.0
    poller_manager._sessions["lagtest"] = session
    try:
        body = client.get("/api/games/lagtest/lag").json()
    finally:
        del poller_manager._sessions["lagtest"]
    assert body["polls"] == 5 and body["median_feed_delay_sec"] == 5.0
    assert body["last_feed_age_seconds"] == 2.1 and body["base_interval_seconds"] == 3.0


def test_scoreboard_reports_heat_checks_for_followed_games(monkeypatch):
    from app.core.game_state import AnomalyEvent, game_state_store

    payload = {
        "scoreboard": {
            "gameDate": "2026-11-12",
            "games": [
                {"gameId": "0022600777", "gameStatus": 2},
                {"gameId": "0022600778", "gameStatus": 2},
            ],
        }
    }
    monkeypatch.setattr("app.nba.client._client", _FakeClient(payload))
    state = game_state_store.get_or_create("0022600777")
    state.anomalies.append(AnomalyEvent(100.0, "home", 8, 20.0, 2.0, "exact", "m"))
    try:
        games = {g["game_id"]: g for g in client.get("/api/scoreboard").json()["games"]}
    finally:
        game_state_store.remove("0022600777")
    assert games["0022600777"]["heat_checks"] == 1
    assert games["0022600778"]["heat_checks"] is None  # not followed


def test_game_state_endpoint_is_compact_and_reports_fidelity():
    from app.core.game_state import game_state_store

    state = game_state_store.get_or_create("0022600555")
    state.home_team, state.away_team = "BOS", "NYK"
    try:
        body = client.get("/api/games/0022600555/state").json()
    finally:
        game_state_store.remove("0022600555")
    assert body["fidelity"] == "play_by_play" and body["anomalies"] == [] and "log" not in body

from fastapi.testclient import TestClient

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


def test_unknown_game_state_is_404():
    resp = client.get("/api/games/nonexistent/state")
    assert resp.status_code == 404

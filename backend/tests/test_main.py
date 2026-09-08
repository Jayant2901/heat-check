from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_live_games_returns_list_even_offseason(monkeypatch):
    async def fake_list_live_game_ids():
        return []

    monkeypatch.setattr("app.api.games.list_live_game_ids", fake_list_live_game_ids)
    resp = client.get("/api/games/live")
    assert resp.status_code == 200
    assert resp.json() == {"game_ids": []}


def test_unknown_game_state_is_404():
    resp = client.get("/api/games/nonexistent/state")
    assert resp.status_code == 404

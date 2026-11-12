import asyncio
import dataclasses
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.api.stream as stream_api
import app.nba.client as nba_client
import app.nba.weekly as weekly
from app.core.artifacts import get_baseline, get_model
from app.main import app
from app.nba.demo import DemoNbaClient
from app.nba.weekly import WeeklyService
from app.polling.poller import poller_manager

TODAY = date(2026, 11, 4)


@pytest.fixture
def demo(monkeypatch):
    client = DemoNbaClient(today=lambda: TODAY)
    monkeypatch.setattr(nba_client, "_client", client)
    weekly.reset_weekly_service()
    yield client
    weekly.reset_weekly_service()


def test_scoreboard_is_ordered_and_carries_mode(demo):
    resp = TestClient(app).get("/api/scoreboard")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "demo"
    assert body["game_date"] == TODAY.isoformat()
    statuses = [g["status"] for g in body["games"]]
    assert statuses == sorted(statuses, key=["live", "scheduled", "final"].index)
    assert statuses[:2] == ["live", "live"]
    assert all(g["tracked"] is False for g in body["games"])
    live = body["games"][0]
    assert live["clock_text"] and live["home_leader"]["name"]


def test_weekly_payload_is_complete_and_sorted(demo):
    service = WeeklyService(demo, get_model(), get_baseline(), today=lambda: TODAY)
    payload = asyncio.run(service.build())

    assert payload["status"] == "ready"
    assert payload["mode"] == "demo"
    assert payload["window"] == {"start": "2026-10-28", "end": "2026-11-04"}
    assert payload["games_analyzed"] == len(payload["games"]) > 0
    assert payload["games_skipped"] == 0

    top = payload["top_performances"]
    assert len(top) == 10
    scores = [p["game_score"] for p in top]
    assert scores == sorted(scores, reverse=True)
    expected_keys = {
        "name", "team", "opponent", "date", "game_id", "minutes", "pts", "reb", "ast",
        "stl", "blk", "fg", "game_score", "result", "line",
    }
    assert set(top[0]) == expected_keys
    assert top[0]["result"].split()[0] in ("W", "L")
    assert "-" in top[0]["fg"]

    for stat, key in (("points", "pts"), ("rebounds", "reb"), ("assists", "ast")):
        values = [row["value"] for row in payload["leaders"][stat]]
        assert len(values) == 3 and values == sorted(values, reverse=True)

    averages = payload["week_averages"]
    assert 0 < len(averages) <= 5
    assert all(a["games"] >= 2 for a in averages)
    assert [a["avg_game_score"] for a in averages] == sorted((a["avg_game_score"] for a in averages), reverse=True)

    swings = payload["moments"]["swings"]
    runs = payload["moments"]["runs"]
    assert 0 < len(swings) <= 8 and 0 < len(runs) <= 6
    assert [s["swing"] for s in swings] == sorted((s["swing"] for s in swings), reverse=True)
    assert [r["z_score"] for r in runs] == sorted((r["z_score"] for r in runs), reverse=True)
    assert all(" at " in m["matchup"] and m["date"] for m in swings + runs)

    dates = [g["date"] for g in payload["games"]]
    assert dates == sorted(dates, reverse=True)
    card = payload["games"][0]
    assert {"game_id", "home", "away", "home_score", "away_score", "lead_changes", "largest_lead", "top_performer"} <= set(card)


def test_weekly_skips_a_failing_game(demo):
    class Flaky(DemoNbaClient):
        async def playbyplay(self, game_id):
            if game_id.endswith("-0"):
                raise RuntimeError("boom")
            return await super().playbyplay(game_id)

    service = WeeklyService(Flaky(today=lambda: TODAY), get_model(), get_baseline(), today=lambda: TODAY)
    payload = asyncio.run(service.build())
    assert payload["games_skipped"] > 0
    assert payload["games_analyzed"] > 0
    assert all(not g["game_id"].endswith("-0") for g in payload["games"])


def test_weekly_endpoint_builds_in_the_background(demo):
    with TestClient(app) as client:
        first = client.get("/api/dashboard/week").json()
        assert first["status"] in ("building", "ready")
        for _ in range(100):
            body = client.get("/api/dashboard/week").json()
            if body["status"] == "ready":
                break
            time.sleep(0.05)
        assert body["status"] == "ready"
        assert body["mode"] == "demo"


def test_model_info_reads_the_meta_files():
    body = TestClient(app).get("/api/model-info").json()
    assert body["model"] == "hist_gradient_boosting"
    assert body["test_season"] == "2025-26"
    assert body["accuracy"] == pytest.approx(0.7473, abs=1e-4)
    assert body["log_loss"] == pytest.approx(0.4772, abs=1e-4)
    assert body["logistic_log_loss"] == pytest.approx(0.4963, abs=1e-4)
    assert body["brier"] == pytest.approx(0.1621, abs=1e-4)
    assert body["n_test_rows"] == 665170
    assert body["n_runs"] == 33761
    assert "2025-26" in body["seasons"]
    assert body["live_poll_interval_seconds"] > 0
    assert "calibration" in body and "heat_check_backtest" in body  # None until the scripts have been run


def test_unknown_stream_id_is_404_and_starts_nothing(demo):
    before = set(poller_manager.active_keys())
    resp = TestClient(app).get("/api/games/not-a-game/stream")
    assert resp.status_code == 404
    assert set(poller_manager.active_keys()) == before


def test_live_game_cap_returns_429(demo, monkeypatch):
    monkeypatch.setattr(stream_api, "settings", dataclasses.replace(stream_api.settings, max_concurrent_live_games=2))
    fake = ["0000000001", "0000000002"]
    for key in fake:
        poller_manager._tasks[key] = object()
    try:
        resp = TestClient(app).get("/api/games/demo-live-1/stream")
        assert resp.status_code == 429
        assert not poller_manager.is_active("demo-live-1")
    finally:
        for key in fake:
            del poller_manager._tasks[key]

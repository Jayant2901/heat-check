"""The SSE endpoint: backlog, live hand-off, reconnect, transport headers.

These drive the real FastAPI app through httpx's ASGI transport. That
transport hands back the whole response once the app finishes it, which works
because every stream here ends at game_end.
"""

import asyncio
import json

import httpx
import pandas as pd
import pytest

from app.api import stream as stream_module
from app.core.game_state import game_state_store
from app.core.interfaces import GameSnapshot
from app.datasources.replay_source import ReplaySource
from app.main import app
from app.polling.poller import poller_manager

EMPTY_BASELINE = pd.DataFrame(
    [], columns=["period", "time_bucket", "magnitude", "mean_log_duration", "std_log_duration", "n"]
)


class FakeModel:
    def predict_proba(self, X):
        return [[0.55, 0.45]] * len(X)


def _snap(period, clock, home, away, status="live"):
    return {
        "game_id": "T",
        "period": period,
        "game_clock_seconds_remaining": clock,
        "home_team": "HOM",
        "away_team": "AWY",
        "home_score": home,
        "away_score": away,
        "game_status": status,
    }


def _game(n_plays=40):
    """A short fake game: n_plays plays, home scoring 2 each, then final."""
    snaps = [_snap(1, 720 - 10 * i, 2 * i, 0) for i in range(n_plays)]
    snaps.append(_snap(4, 0, 2 * n_plays, 0, status="final"))
    return snaps


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            fields[key] = value.lstrip(" ")
        if "event" in fields or "data" in fields:
            events.append(fields)
    return events


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    # sse-starlette keeps its shutdown event in a module global that binds to
    # the first event loop that uses it; every test here has its own loop.
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    monkeypatch.setattr(stream_module, "get_model", lambda: FakeModel())
    monkeypatch.setattr(stream_module, "get_baseline", lambda: EMPTY_BASELINE)
    yield
    for key in list(game_state_store.all_ids()):
        if key.startswith("S-") or key.startswith("replay:"):
            poller_manager.stop(key)
            game_state_store.remove(key)


@pytest.fixture
def api():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _finish(key):
    for _ in range(500):
        if not poller_manager.is_active(key):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{key} never finished")


def _ids(events):
    return [int(e["id"]) for e in events if "id" in e]


@pytest.mark.asyncio
async def test_late_joiner_gets_the_whole_game_exactly_once(api):
    """The poller has finished and exited; the cached state still gives a new
    visitor every point, in order, then game_end."""
    source = ReplaySource(snapshots=_game())
    poller_manager.start("S-LATE", source, FakeModel(), EMPTY_BASELINE, 0.001, "S-LATE")
    await _finish("S-LATE")
    state = game_state_store.get("S-LATE")
    assert state is not None and state.status == "final"  # kept, not deleted

    async with api as client:
        response = await client.get("/api/games/S-LATE/stream")
    events = parse_sse(response.text)
    kinds = [e.get("event") for e in events if "event" in e]
    assert kinds.count("wp_update") == len(state.score_history) == 41
    assert kinds[-1] == "game_end"
    assert _ids(events) == list(range(1, 43))  # contiguous: nothing lost, nothing twice
    first = json.loads(next(e for e in events if e.get("event") == "wp_update")["data"])
    assert {"run", "period", "clock", "wp_away", "fidelity", "server_ts"} <= set(first)  # backlog == live shape
    assert first["backlog"] is True


@pytest.mark.asyncio
async def test_backlog_to_live_hand_off_loses_nothing_and_repeats_nothing(api):
    """Connect while the poller is still publishing."""
    source = ReplaySource(snapshots=_game(120))
    poller_manager.start("S-MID", source, FakeModel(), EMPTY_BASELINE, 0.004, "S-MID")
    await asyncio.sleep(0.15)  # join part-way through
    assert poller_manager.is_active("S-MID")
    async with api as client:
        response = await client.get("/api/games/S-MID/stream")
    events = parse_sse(response.text)
    ids = _ids(events)
    assert ids == list(range(1, 123))
    flags = [json.loads(e["data"])["backlog"] for e in events if "event" in e]
    assert flags[0] is True and flags[-1] is False  # history first, then plays that happened while connected
    assert flags == sorted(flags, reverse=True)  # never live-then-history
    assert [e["event"] for e in events if "event" in e][-1] == "game_end"


@pytest.mark.asyncio
async def test_reconnect_with_last_event_id_sends_only_what_was_missed(api):
    poller_manager.start("S-RECON", ReplaySource(snapshots=_game()), FakeModel(), EMPTY_BASELINE, 0.001, "S-RECON")
    await _finish("S-RECON")
    async with api as client:
        response = await client.get("/api/games/S-RECON/stream", headers={"Last-Event-ID": "30"})
    ids = _ids(parse_sse(response.text))
    assert ids == list(range(31, 43))


@pytest.mark.asyncio
async def test_an_event_id_from_the_future_gets_the_full_backlog(api):
    """After a server restart the game is rebuilt with smaller ids; a stale
    Last-Event-ID must not make the client miss everything."""
    poller_manager.start("S-FUT", ReplaySource(snapshots=_game()), FakeModel(), EMPTY_BASELINE, 0.001, "S-FUT")
    await _finish("S-FUT")
    async with api as client:
        response = await client.get("/api/games/S-FUT/stream", headers={"Last-Event-ID": "99999"})
    assert _ids(parse_sse(response.text)) == list(range(1, 43))


@pytest.mark.asyncio
async def test_stream_headers_are_not_cacheable_buffered_or_compressed(api):
    poller_manager.start("S-HDR", ReplaySource(snapshots=_game(5)), FakeModel(), EMPTY_BASELINE, 0.001, "S-HDR")
    await _finish("S-HDR")
    async with api as client:
        response = await client.get("/api/games/S-HDR/stream", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["connection"] == "keep-alive"
    assert "content-encoding" not in response.headers
    assert "retry:" in response.text  # the reconnect hint


@pytest.mark.asyncio
async def test_other_responses_are_still_compressed(api):
    async with api as client:
        response = await client.get("/src/styles.css", headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"


@pytest.mark.asyncio
async def test_unknown_replay_session_is_404(api):
    async with api as client:
        response = await client.get("/api/games/replay:nope:1/stream")
    assert response.status_code == 404


class _Backfilling:
    """A live source that returns the whole game on its first poll."""

    backfills = True

    def __init__(self, snapshots, fail=False):
        self._snapshots = [GameSnapshot(**{"exact": True, **s}) for s in snapshots]
        self._fail = fail

    async def next_snapshots(self, game_id):
        if self._fail:
            return None
        batch, self._snapshots = self._snapshots, []
        return batch


class _Client:
    mode = "live"

    def __init__(self, source):
        self._source = source

    async def scoreboard(self):
        return {
            "scoreboard": {
                "gameDate": "2026-11-12",
                "games": [
                    {
                        "gameId": "S-NEW",
                        "gameStatus": 2,
                        "homeTeam": {"teamTricode": "HOM"},
                        "awayTeam": {"teamTricode": "AWY"},
                    }
                ],
            }
        }

    def live_source(self, game_id, teams=None):
        self.teams = teams
        return self._source, 0.5


@pytest.mark.asyncio
async def test_first_viewer_of_a_game_in_progress_gets_the_backlog_before_the_stream_starts(api, monkeypatch):
    """start_primed runs the first poll (the whole play-by-play so far) before
    the response begins, so even the FIRST viewer sees the full chart."""
    client_stub = _Client(_Backfilling(_game(30)))
    monkeypatch.setattr(stream_module, "get_nba_client", lambda: client_stub)
    async with api as client:
        response = await client.get("/api/games/S-NEW/stream")
    assert client_stub.teams == ("HOM", "AWY")
    events = parse_sse(response.text)
    assert _ids(events) == list(range(1, 33))
    assert [e["event"] for e in events if "event" in e][-1] == "game_end"


@pytest.mark.asyncio
async def test_two_simultaneous_first_viewers_share_one_poller(api, monkeypatch):
    sources = []

    class Counting(_Client):
        def live_source(self, game_id, teams=None):
            source = _Backfilling(_game(10))
            sources.append(source)
            return source, 0.5

    monkeypatch.setattr(stream_module, "get_nba_client", lambda: Counting(None))
    async with api as client:
        first, second = await asyncio.gather(client.get("/api/games/S-NEW/stream"), client.get("/api/games/S-NEW/stream"))
    assert _ids(parse_sse(first.text)) == _ids(parse_sse(second.text)) == list(range(1, 13))
    assert len(sources) <= 2  # the second request may build a source, but only one is ever polled
    assert poller_manager.stats("S-NEW")["polls"] >= 1


@pytest.mark.asyncio
async def test_a_feed_that_cannot_be_read_is_a_503(api, monkeypatch):
    monkeypatch.setattr(stream_module, "get_nba_client", lambda: _Client(_Backfilling([], fail=True)))
    async with api as client:
        response = await client.get("/api/games/S-NEW/stream")
    assert response.status_code == 503
    assert "live feed" in response.json()["detail"]
    assert game_state_store.get("S-NEW") is None


@pytest.mark.asyncio
async def test_a_game_not_on_the_scoreboard_is_404(api, monkeypatch):
    monkeypatch.setattr(stream_module, "get_nba_client", lambda: _Client(None))
    async with api as client:
        response = await client.get("/api/games/S-OTHER/stream")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_too_many_open_streams_from_one_client_is_429(api, monkeypatch):
    poller_manager.start("S-LIM", ReplaySource(snapshots=_game(5)), FakeModel(), EMPTY_BASELINE, 0.001, "S-LIM")
    await _finish("S-LIM")
    limiter = stream_module.stream_limiter
    monkeypatch.setattr(limiter, "per_client", 0)
    async with api as client:
        response = await client.get("/api/games/S-LIM/stream")
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_stream_slots_are_released_when_a_stream_ends(api):
    limiter = stream_module.stream_limiter
    before = limiter.total
    poller_manager.start("S-REL", ReplaySource(snapshots=_game(5)), FakeModel(), EMPTY_BASELINE, 0.001, "S-REL")
    await _finish("S-REL")
    async with api as client:
        await client.get("/api/games/S-REL/stream")
    assert limiter.total == before


def test_event_to_sse_carries_the_sequence_number_as_the_event_id():
    from app.core.events import GameEndEvent

    message = stream_module.event_to_sse(GameEndEvent(type="game_end", final_home_score=1, final_away_score=0, winner="HOM", seq=7))
    assert message["event"] == "game_end" and message["id"] == "7"
    assert json.loads(message["data"])["winner"] == "HOM"

import httpx
import pytest

import app.nba.client as nba_client
from app.nba.client import SCOREBOARD_URL, HttpNbaClient, NbaUnavailable


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(nba_client.asyncio, "sleep", instant)


def _client(handler):
    return HttpNbaClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_sends_browser_headers_and_retries():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"scoreboard": {"games": []}})

    client = _client(handler)
    assert await client.scoreboard() == {"scoreboard": {"games": []}}
    assert len(calls) == 3
    assert str(calls[0].url) == SCOREBOARD_URL
    assert "Mozilla" in calls[0].headers["User-Agent"]
    assert "origin" not in calls[0].headers  # matches nba_api's live headers
    await client.aclose()


@pytest.mark.asyncio
async def test_scoreboard_is_cached_and_a_stale_copy_survives_an_outage(monkeypatch):
    state = {"up": True, "calls": 0}

    def handler(request):
        state["calls"] += 1
        if not state["up"]:
            return httpx.Response(500)
        return httpx.Response(200, json={"n": state["calls"]})

    client = _client(handler)
    first = await client.scoreboard()
    assert await client.scoreboard() == first  # within the TTL: no refetch
    assert state["calls"] == 1

    monkeypatch.setattr(nba_client, "SCOREBOARD_TTL_SECONDS", -1.0)  # force expiry
    state["up"] = False
    assert await client.scoreboard() == first  # stale copy served
    assert state["calls"] == 1 + nba_client.RETRIES
    await client.aclose()


@pytest.mark.asyncio
async def test_boxscore_failure_raises_after_retries():
    client = _client(lambda request: httpx.Response(404))
    with pytest.raises(NbaUnavailable):
        await client.boxscore("0022600001")
    await client.aclose()


@pytest.mark.asyncio
async def test_failures_carry_a_readable_reason():
    client = _client(lambda request: httpx.Response(403))
    with pytest.raises(NbaUnavailable) as info:
        await client.scoreboard()
    assert info.value.reason == "HTTP 403"
    await client.aclose()


def test_headers_send_no_cross_site_origin():
    from app.nba.client import HEADERS

    assert "Origin" not in HEADERS
    assert "br" not in HEADERS["Accept-Encoding"]


@pytest.mark.asyncio
async def test_probe_reports_each_endpoint():
    def handler(request):
        if "todaysScoreboard" in str(request.url):
            return httpx.Response(200, json={"scoreboard": {"games": [{"gameId": "0022600001"}]}})
        if "schedule" in str(request.url):
            return httpx.Response(403)
        return httpx.Response(200, json={"game": {}})

    client = _client(handler)
    checks = await client.probe()
    assert [c["name"] for c in checks] == ["scoreboard", "schedule", "boxscore"]
    assert checks[0]["ok"] and checks[0]["status"] == 200
    assert checks[1] == {**checks[1], "ok": False, "status": 403, "error": "HTTP 403"}
    assert checks[2]["url"].endswith("boxscore_0022600001.json")
    await client.aclose()

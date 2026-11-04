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
    assert calls[0].headers["Referer"] == "https://www.nba.com/"
    assert "Mozilla" in calls[0].headers["User-Agent"]
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

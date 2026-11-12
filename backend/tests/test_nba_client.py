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
    assert calls[0].headers["Referer"] == "https://www.nba.com/"
    assert calls[0].headers["Origin"] == "https://www.nba.com"
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


def test_browser_headers_can_be_switched_off(monkeypatch):
    from app.nba.client import request_headers

    assert "Origin" in request_headers()
    import dataclasses

    monkeypatch.setattr(nba_client, "settings", dataclasses.replace(nba_client.settings, nba_browser_headers=False))
    assert "Origin" not in request_headers() and "Referer" not in request_headers()
    assert "br" not in request_headers()["Accept-Encoding"]  # httpx can't decode brotli without the extra


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
    assert [c["name"] for c in checks] == ["nba scoreboard", "nba schedule", "nba boxscore", "nba playbyplay"]
    assert checks[0]["ok"] and checks[0]["status"] == 200
    assert checks[1] == {**checks[1], "ok": False, "status": 403, "error": "HTTP 403"}
    assert checks[2]["url"].endswith("boxscore_0022600001.json")
    assert checks[3]["url"].endswith("playbyplay_0022600001.json")
    await client.aclose()


# -- conditional requests for live feeds ------------------------------------


@pytest.mark.asyncio
async def test_an_unchanged_feed_costs_a_304_and_returns_the_copy_we_have():
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(200, json={"game": {"actions": [1, 2, 3]}}, headers={"etag": '"v1"'})

    client = _client(handler)
    first = await client.playbyplay_feed("0022600001")
    second = await client.playbyplay_feed("0022600001")
    assert first.not_modified is False and second.not_modified is True
    assert second.data == {"game": {"actions": [1, 2, 3]}}
    assert seen == [None, '"v1"']
    await client.aclose()


@pytest.mark.asyncio
async def test_last_modified_is_used_when_there_is_no_etag():
    seen = []

    def handler(request):
        seen.append(request.headers.get("if-modified-since"))
        if request.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(200, json={"n": 1}, headers={"last-modified": "Thu, 12 Nov 2026 01:00:00 GMT"})

    client = _client(handler)
    await client.boxscore_feed("g")
    assert (await client.boxscore_feed("g")).not_modified
    assert seen == [None, "Thu, 12 Nov 2026 01:00:00 GMT"]
    await client.aclose()


@pytest.mark.asyncio
async def test_a_live_feed_fetch_does_not_retry_or_sleep():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    client = _client(handler)
    with pytest.raises(NbaUnavailable) as info:
        await client.playbyplay_feed("g")
    assert len(calls) == 1  # the poller's next tick is the retry; sleeping here would add lag
    assert info.value.reason == "HTTP 503"
    await client.aclose()


@pytest.mark.asyncio
async def test_a_fresh_cache_control_skips_the_request(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"n": len(calls)}, headers={"cache-control": "public, max-age=30"})

    client = _client(handler)
    first = await client.playbyplay_feed("g")
    second = await client.playbyplay_feed("g")  # inside the (capped) freshness window
    assert len(calls) == 1 and second.from_cache and second.data == first.data
    await client.aclose()


def test_fresh_seconds_reads_cache_control_and_caps_it():
    from app.nba.client import LIVE_MAX_FRESH_SECONDS, fresh_seconds

    assert fresh_seconds(None) == 0 and fresh_seconds("") == 0
    assert fresh_seconds("no-cache") == 0 and fresh_seconds("no-store, max-age=10") == 0
    assert fresh_seconds("public, max-age=1") == 1
    assert fresh_seconds("public, max-age=600") == LIVE_MAX_FRESH_SECONDS
    assert fresh_seconds("s-maxage=1, max-age=5") == LIVE_MAX_FRESH_SECONDS  # s-maxage is for shared caches
    assert fresh_seconds("s-maxage=1") == 0


@pytest.mark.asyncio
async def test_http_client_builds_a_play_by_play_live_source():
    from app.datasources.nba_live import PlayByPlayLiveSource

    source, interval = HttpNbaClient().live_source("0022600001", ("BOS", "NYK"))
    assert isinstance(source, PlayByPlayLiveSource) and source.backfills
    assert interval == nba_client.settings.live_poll_interval_seconds


@pytest.mark.asyncio
async def test_remembered_feed_validators_are_bounded():
    client = _client(lambda request: httpx.Response(200, json={"n": 1}, headers={"etag": '"x"'}))
    for n in range(nba_client.MAX_VALIDATORS + 10):
        await client.fetch_feed(f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{n}.json")
    assert len(client._validators) <= nba_client.MAX_VALIDATORS
    await client.aclose()

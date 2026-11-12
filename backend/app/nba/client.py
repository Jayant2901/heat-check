"""Where NBA data comes from, picked by settings:

- data_mode "demo": generated sample data (DemoNbaClient, app/nba/demo.py).
- otherwise AutoNbaClient, which uses cdn.nba.com (HttpNbaClient) and falls
  back to ESPN (EspnNbaClient, app/nba/espn.py) when NBA.com refuses or
  can't be reached -- cdn.nba.com answers HTTP 403 to some cloud hosts.
  NBA_SOURCE=nba or NBA_SOURCE=espn pins one source.

stats.nba.com is never used (it blocks most cloud hosts).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import settings
from app.core.interfaces import DataSource

logger = logging.getLogger(__name__)

CDN_BASE = "https://cdn.nba.com/static/json"
SCOREBOARD_URL = f"{CDN_BASE}/liveData/scoreboard/todaysScoreboard_00.json"
SCHEDULE_URL = f"{CDN_BASE}/staticData/scheduleLeagueV2.json"
BOXSCORE_URL = f"{CDN_BASE}/liveData/boxscore/boxscore_{{game_id}}.json"
PLAYBYPLAY_URL = f"{CDN_BASE}/liveData/playbyplay/playbyplay_{{game_id}}.json"

# The header set nba_api's live endpoints send (nba_api/live/nba/library/
# http.py). Accept-Encoding omits "br" because httpx can only decode brotli if
# it is installed. On top of that, NBA_BROWSER_HEADERS (default on) adds a
# Referer/Origin of https://www.nba.com, which clears some 403s from cloud
# hosts. That addition is UNVERIFIED: it could not be tested from the
# environment this was written in (see README, "What is verified").
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}
BROWSER_HEADERS = {
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
}


def request_headers() -> dict[str, str]:
    headers = dict(BASE_HEADERS)
    if settings.nba_browser_headers:
        headers.update(BROWSER_HEADERS)
    return headers


# Back-compat name for tests and callers that read the default header set.
HEADERS = request_headers()

# A live poll skips the request only if the CDN said the copy is still fresh
# for this long at most -- polls are ~3 s apart, so this never makes a live
# poll wait; it only stops a burst of callers refetching the same URL.
LIVE_MAX_FRESH_SECONDS = 1.5
MAX_VALIDATORS = 16  # live feeds kept for conditional requests (a few per followed game)

SCOREBOARD_TTL_SECONDS = 10.0
SCHEDULE_TTL_SECONDS = 30 * 60.0
RETRIES = 3
CACHE_PRUNE_ABOVE = 64
CACHE_MAX_AGE_SECONDS = 3600.0
# After NBA.com fails, use the fallback for this long before trying again,
# so every page load doesn't wait on a request that will be refused.
PRIMARY_RETRY_AFTER_SECONDS = 600.0


class NbaUnavailable(Exception):
    """NBA.com could not be reached (after retries) and no stale copy exists.

    `reason` is a short, user-readable cause such as "HTTP 403" or
    "timed out", so error messages can say why instead of just "unreachable".
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


def describe_error(exc: BaseException) -> str:
    """A short cause for a failed fetch: "HTTP 403", "timed out", ..."""
    if isinstance(exc, NbaUnavailable) and exc.reason:
        return exc.reason
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect"
    if isinstance(exc, ValueError):
        return "response was not JSON"
    return type(exc).__name__


@dataclass(frozen=True)
class FeedResult:
    """One conditional fetch of a live feed.

    `not_modified` is True when the CDN answered 304 (or said the copy is
    still fresh): `data` is then the copy fetched earlier. `fetch_ms` is the
    time the request took (0 when nothing was requested).
    """

    data: dict
    not_modified: bool = False
    fetch_ms: float = 0.0
    from_cache: bool = False


@dataclass
class _Validator:
    data: dict
    etag: str | None
    last_modified: str | None
    fresh_until: float


_MAX_AGE = re.compile(r"(?:^|[,\s])max-age=(\d+)", re.IGNORECASE)  # s-maxage is for shared caches, not us


def fresh_seconds(cache_control: str | None) -> float:
    """How long the response's Cache-Control says it stays fresh, capped at
    LIVE_MAX_FRESH_SECONDS. 0 when it says no-cache/no-store or says nothing."""
    text = (cache_control or "").lower()
    if not text or "no-cache" in text or "no-store" in text:
        return 0.0
    match = _MAX_AGE.search(text)
    if not match:
        return 0.0
    return min(float(match.group(1)), LIVE_MAX_FRESH_SECONDS)


class NbaClient(Protocol):
    mode: str

    async def scoreboard(self) -> dict: ...

    async def schedule(self) -> dict: ...

    async def boxscore(self, game_id: str) -> dict: ...

    async def playbyplay(self, game_id: str) -> dict: ...

    def live_source(self, game_id: str, teams: tuple[str, str] | None = None) -> tuple[DataSource, float]:
        """The DataSource to poll for this game, and the poll interval.
        `teams` is (home, away) tricodes from the scoreboard, if known."""
        ...


class HttpNbaClient:
    mode = "live"
    source = "nba"

    def __init__(self, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._validators: dict[str, _Validator] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers=request_headers(),
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _fetch(self, url: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            try:
                response = await self._client().get(url)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                # A refusal (403, 404, ...) won't change on retry; a rate
                # limit (429), a server error or a network error might.
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if 400 <= status < 500 and status != 429:
                        break
                if attempt < RETRIES - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
        reason = describe_error(last_error) if last_error else "unknown error"
        logger.warning("Fetch failed: %s (%s)", url, reason)
        raise NbaUnavailable(f"Could not fetch {url}: {last_error}", reason) from last_error

    async def fetch_feed(self, url: str) -> FeedResult:
        """Conditional GET for a live feed, with no internal retries (the
        poller's next tick is the retry, and a sleep here would add to the
        lag). Sends If-None-Match / If-Modified-Since when the last response
        had validators, so an unchanged feed costs a 304 with no body."""
        started = time.perf_counter()
        validator = self._validators.get(url)
        if validator is not None and time.monotonic() < validator.fresh_until:
            return FeedResult(validator.data, not_modified=True, from_cache=True)
        conditional = {}
        if validator is not None:
            if validator.etag:
                conditional["If-None-Match"] = validator.etag
            elif validator.last_modified:
                conditional["If-Modified-Since"] = validator.last_modified
        try:
            response = await self._client().get(url, headers=conditional)
            if response.status_code == 304 and validator is not None:
                validator.fresh_until = time.monotonic() + fresh_seconds(response.headers.get("cache-control"))
                return FeedResult(validator.data, True, (time.perf_counter() - started) * 1000)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            reason = describe_error(exc)
            logger.warning("Feed fetch failed: %s (%s)", url, reason)
            raise NbaUnavailable(f"Could not fetch {url}: {exc}", reason) from exc
        while len(self._validators) >= MAX_VALIDATORS:  # whole-game ESPN summaries are ~450 KB each
            self._validators.pop(next(iter(self._validators)))
        self._validators[url] = _Validator(
            data=data,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            fresh_until=time.monotonic() + fresh_seconds(response.headers.get("cache-control")),
        )
        return FeedResult(data, False, (time.perf_counter() - started) * 1000)

    async def playbyplay_feed(self, game_id: str) -> FeedResult:
        return await self.fetch_feed(PLAYBYPLAY_URL.format(game_id=game_id))

    async def boxscore_feed(self, game_id: str) -> FeedResult:
        return await self.fetch_feed(BOXSCORE_URL.format(game_id=game_id))

    async def _cached(self, url: str, ttl: float) -> dict:
        lock = self._locks.setdefault(url, asyncio.Lock())
        # The lock stops a burst of requests (e.g. several open tabs) from
        # all refetching the same expired URL at once.
        async with lock:
            cached = self._cache.get(url)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
            try:
                data = await self._fetch(url)
            except NbaUnavailable:
                if cached is not None:
                    logger.warning("Serving a stale copy of %s", url)
                    return cached[1]
                raise
            now = time.monotonic()
            self._cache[url] = (now, data)
            self._prune(now)
            return data

    def _prune(self, now: float) -> None:
        """Keep the cache bounded: drop anything older than an hour once it
        grows (per-game summaries would otherwise pile up over days)."""
        if len(self._cache) <= CACHE_PRUNE_ABOVE:
            return
        for url, (stored, _data) in list(self._cache.items()):
            if now - stored > CACHE_MAX_AGE_SECONDS:
                del self._cache[url]
                self._locks.pop(url, None)

    async def _probe_one(self, name: str, url: str) -> tuple[dict, dict | None]:
        """One uncached request, reported as {name, url, ok, status, error, ms}."""
        started = time.monotonic()
        result = {"name": name, "url": url, "ok": False, "status": None, "error": None}
        body = None
        try:
            response = await self._client().get(url)
            result["status"] = response.status_code
            response.raise_for_status()
            body = response.json()
            result["ok"] = True
            result["bytes"] = len(response.content)
        except Exception as exc:
            result["error"] = describe_error(exc)
        result["ms"] = round((time.monotonic() - started) * 1000)
        return result, body

    async def probe(self) -> list[dict]:
        """One uncached request each to the scoreboard, the schedule and one
        boxscore, reporting what came back. Backs /api/nba-check, so a deploy
        can show exactly why NBA.com data isn't loading."""
        board_result, board = await self._probe_one("nba scoreboard", SCOREBOARD_URL)
        schedule_result, _ = await self._probe_one("nba schedule", SCHEDULE_URL)
        results = [board_result, schedule_result]
        games = ((board or {}).get("scoreboard") or {}).get("games") or []
        if games:
            url = BOXSCORE_URL.format(game_id=games[0].get("gameId"))
            box_result, _ = await self._probe_one("nba boxscore", url)
            results.append(box_result)
            pbp_url = PLAYBYPLAY_URL.format(game_id=games[0].get("gameId"))
            pbp_result, _ = await self._probe_one("nba playbyplay", pbp_url)
            results.append(pbp_result)
        return results

    async def scoreboard(self) -> dict:
        return await self._cached(SCOREBOARD_URL, SCOREBOARD_TTL_SECONDS)

    async def schedule(self) -> dict:
        return await self._cached(SCHEDULE_URL, SCHEDULE_TTL_SECONDS)

    async def boxscore(self, game_id: str) -> dict:
        return await self._fetch(BOXSCORE_URL.format(game_id=game_id))

    async def playbyplay(self, game_id: str) -> dict:
        return await self._fetch(PLAYBYPLAY_URL.format(game_id=game_id))

    def live_source(self, game_id: str, teams: tuple[str, str] | None = None) -> tuple[DataSource, float]:
        from app.datasources.nba_live import PlayByPlayLiveSource

        home, away = teams or ("", "")
        source = PlayByPlayLiveSource(self.playbyplay_feed, self.boxscore_feed, home=home, away=away)
        return source, settings.live_poll_interval_seconds


class AutoNbaClient:
    """cdn.nba.com first, ESPN when NBA.com fails.

    Today's scoreboard and the schedule come from whichever source is
    currently working. After NBA.com fails, the fallback is used for
    PRIMARY_RETRY_AFTER_SECONDS before NBA.com is tried again. Per-game
    calls (boxscore, play-by-play, live polling) go to the source the game
    id belongs to: NBA.com ids start "00", ESPN event ids don't.
    """

    mode = "live"

    def __init__(self, primary, fallback, prefer: str = "auto") -> None:
        self.primary = primary
        self.fallback = fallback
        self.prefer = prefer
        self._primary_down_until = 0.0
        self.active = fallback.source if prefer == fallback.source else primary.source

    @property
    def source(self) -> str:
        return self.active

    def _for_id(self, game_id: str):
        from app.nba.espn import is_espn_id

        return self.fallback if is_espn_id(game_id) else self.primary

    async def _either(self, method: str) -> dict:
        if self.prefer == self.primary.source:
            return await getattr(self.primary, method)()
        if self.prefer == self.fallback.source:
            return await getattr(self.fallback, method)()
        if time.monotonic() >= self._primary_down_until:
            try:
                data = await getattr(self.primary, method)()
                self.active = self.primary.source
                return data
            except NbaUnavailable as exc:
                logger.warning("NBA.com %s failed (%s); using ESPN", method, exc.reason)
                self._primary_down_until = time.monotonic() + PRIMARY_RETRY_AFTER_SECONDS
        data = await getattr(self.fallback, method)()
        self.active = self.fallback.source
        return data

    async def scoreboard(self) -> dict:
        return await self._either("scoreboard")

    async def schedule(self) -> dict:
        return await self._either("schedule")

    async def boxscore(self, game_id: str) -> dict:
        return await self._for_id(game_id).boxscore(game_id)

    async def playbyplay(self, game_id: str) -> dict:
        return await self._for_id(game_id).playbyplay(game_id)

    def live_source(self, game_id: str, teams: tuple[str, str] | None = None) -> tuple[DataSource, float]:
        return self._for_id(game_id).live_source(game_id, teams)

    async def probe(self) -> list[dict]:
        return [*await self.primary.probe(), *await self.fallback.probe()]


_client: NbaClient | None = None


def get_nba_client() -> NbaClient:
    global _client
    if _client is None:
        if settings.data_mode == "demo":
            from app.nba.demo import DemoNbaClient

            _client = DemoNbaClient()
        else:
            from app.nba.espn import EspnNbaClient

            _client = AutoNbaClient(HttpNbaClient(), EspnNbaClient(), prefer=settings.nba_source)
    return _client

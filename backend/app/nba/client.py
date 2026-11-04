"""Where NBA data comes from: cdn.nba.com (HttpNbaClient) or generated sample
data (DemoNbaClient, app/nba/demo.py), picked by settings.data_mode.

Only cdn.nba.com is used -- stats.nba.com blocks most cloud hosts.
"""

import asyncio
import logging
import time
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

# The same header set nba_api's live endpoints send (nba_api/live/nba/library/
# http.py), which is known to be accepted by cdn.nba.com from servers. It
# deliberately has no Origin/Referer: a cross-site Origin from a server is
# the kind of request the CDN's bot protection can reject. Accept-Encoding
# omits "br" because httpx can only decode brotli if it is installed.
HEADERS = {
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

SCOREBOARD_TTL_SECONDS = 10.0
SCHEDULE_TTL_SECONDS = 30 * 60.0
RETRIES = 3


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


class NbaClient(Protocol):
    mode: str

    async def scoreboard(self) -> dict: ...

    async def schedule(self) -> dict: ...

    async def boxscore(self, game_id: str) -> dict: ...

    async def playbyplay(self, game_id: str) -> dict: ...

    def live_source(self, game_id: str) -> tuple[DataSource, float]:
        """The DataSource to poll for this game, and the poll interval."""
        ...


class HttpNbaClient:
    mode = "live"

    def __init__(self, timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                headers=HEADERS,
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
                if attempt < RETRIES - 1:
                    await asyncio.sleep(0.5 * (2**attempt))
        reason = describe_error(last_error) if last_error else "unknown error"
        logger.warning("NBA.com fetch failed after %d tries: %s (%s)", RETRIES, url, reason)
        raise NbaUnavailable(f"Could not fetch {url}: {last_error}", reason) from last_error

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
            self._cache[url] = (time.monotonic(), data)
            return data

    async def probe(self) -> list[dict]:
        """One uncached request each to the scoreboard, the schedule and one
        boxscore, reporting what came back. Backs /api/nba-check, so a deploy
        can show exactly why NBA.com data isn't loading."""

        async def one(name: str, url: str) -> tuple[dict, dict | None]:
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

        board_result, board = await one("scoreboard", SCOREBOARD_URL)
        schedule_result, _ = await one("schedule", SCHEDULE_URL)
        results = [board_result, schedule_result]
        games = ((board or {}).get("scoreboard") or {}).get("games") or []
        if games:
            box_result, _ = await one("boxscore", BOXSCORE_URL.format(game_id=games[0].get("gameId")))
            results.append(box_result)
        return results

    async def scoreboard(self) -> dict:
        return await self._cached(SCOREBOARD_URL, SCOREBOARD_TTL_SECONDS)

    async def schedule(self) -> dict:
        return await self._cached(SCHEDULE_URL, SCHEDULE_TTL_SECONDS)

    async def boxscore(self, game_id: str) -> dict:
        return await self._fetch(BOXSCORE_URL.format(game_id=game_id))

    async def playbyplay(self, game_id: str) -> dict:
        return await self._fetch(PLAYBYPLAY_URL.format(game_id=game_id))

    def live_source(self, game_id: str) -> tuple[DataSource, float]:
        from app.datasources.nba_live import NbaLiveDataSource

        return NbaLiveDataSource(self.boxscore), settings.poll_interval_seconds


_client: NbaClient | None = None


def get_nba_client() -> NbaClient:
    global _client
    if _client is None:
        if settings.data_mode == "demo":
            from app.nba.demo import DemoNbaClient

            _client = DemoNbaClient()
        else:
            _client = HttpNbaClient()
    return _client

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "Accept": "application/json, text/plain, */*",
}

SCOREBOARD_TTL_SECONDS = 10.0
SCHEDULE_TTL_SECONDS = 30 * 60.0
RETRIES = 3


class NbaUnavailable(Exception):
    """NBA.com could not be reached (after retries) and no stale copy exists."""


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
        raise NbaUnavailable(f"Could not fetch {url}: {last_error}") from last_error

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

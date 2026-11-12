"""Drives one tracked key's DataSource through the engine, on a fixed
cadence, publishing every event to the broadcaster.

The SAME code runs a live game (PlayByPlayLiveSource, one real poll every
live_poll_interval_seconds) and a replay session (ReplaySource, one fixture
step per poll_interval_seconds/speed) -- both are just a DataSource (see
core/interfaces.py). "key" is the GameStateStore/broadcaster identity: for a
live game it's the real game_id; for a replay session it's a namespaced
"replay:{game_id}:{session_id}" so multiple viewers can each run their own
session without colliding. "source_game_id" is what gets passed to the
source -- the real game_id for live, ignored by ReplaySource.

Cadence. Each cycle is scheduled against the START of the previous one
(next_run = cycle_start + interval) and the loop sleeps only for what is
left, so the time a fetch takes does not stretch the period -- the old
"fetch, then sleep(interval)" loop polled every interval + fetch time. A
source may ask for a different interval each cycle (suggest_interval), which
is how a quiet feed (halftime) is polled less often and then speeds up again.

A live poller is started by the first /stream connection and would otherwise
keep polling until the game ends, viewers or not. Passing idle_stop_seconds
makes it stop itself once nobody has been subscribed for that long. Its
state is kept (bounded, see core/game_state.py) so the next viewer still
sees the whole game; a new poller then rebuilds it from the feed.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field

from app.core.config import settings
from app.core.engine import process_snapshots
from app.core.events import DegradedEvent
from app.core.game_state import game_state_store
from app.core.interfaces import DataSource, GameSnapshot
from app.sse.broadcaster import broadcaster

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURES_BEFORE_DEGRADED = 3
MAX_REMEMBERED_SESSIONS = 64


class SourceUnavailable(Exception):
    """The first poll of a newly tracked game failed."""


@dataclass
class PollStats:
    """What /api/games/{id}/lag reports for one tracked key."""

    started_at: float
    polls: int = 0
    failures: int = 0
    not_modified: int = 0
    interval_seconds: float = 0.0
    last_poll_at: float | None = None
    # Whole poll (all requests the source made) and the play-by-play request alone.
    last_poll_ms: float | None = None
    last_fetch_ms: float | None = None
    # Processing + publishing the new events, after the data arrived.
    last_publish_ms: float | None = None
    # Age of the newest play in the feed when it was fetched (feed lag floor).
    last_feed_age_seconds: float | None = None
    last_new_actions: int = 0
    fidelity: str | None = None
    recent_feed_delays: list = field(default_factory=list)


async def _poll(source: DataSource, game_id: str) -> list[GameSnapshot] | None:
    batch = getattr(source, "next_snapshots", None)
    if batch is not None:
        return await batch(game_id)
    snapshot = await source.next_snapshot(game_id)  # a source written for the one-snapshot protocol
    return None if snapshot is None else [snapshot]


class _Session:
    def __init__(self, manager, key, source, model, baseline, interval_seconds, source_game_id, idle_stop_seconds):
        self.m = manager
        self.key = key
        self.source = source
        self.model = model
        self.baseline = baseline
        self.base_interval = interval_seconds
        self.source_game_id = source_game_id
        self.idle_stop_seconds = idle_stop_seconds
        if getattr(source, "backfills", False):
            # This source rebuilds the whole game from the feed, so a stale
            # copy from an earlier poller must not be stacked underneath.
            self.state = game_state_store.reset(key)
        else:
            self.state = game_state_store.get_or_create(key)
        game_state_store.activate(key)
        self.failures = 0
        self.last_watched = manager.clock()
        self.stats = PollStats(started_at=manager.wall())

    def next_interval(self) -> float:
        suggest = getattr(self.source, "suggest_interval", None)
        interval = suggest(self.base_interval) if suggest else self.base_interval
        self.stats.interval_seconds = interval
        return interval

    def idle_expired(self) -> bool:
        if self.idle_stop_seconds is None:
            return False
        now = self.m.clock()
        if broadcaster.subscriber_count(self.key) > 0:
            self.last_watched = now
            return False
        if now - self.last_watched >= self.idle_stop_seconds:
            logger.info("Stopping %s: no viewers for %.0fs", self.key, self.idle_stop_seconds)
            return True
        return False

    async def poll_once(self) -> str:
        """One poll -> engine -> publish. Returns "ok", "failed" or "done"."""
        started = self.m.clock()
        try:
            batch = await _poll(self.source, self.source_game_id)
        except Exception:
            batch = None
            logger.warning("Data source error for %s", self.key, exc_info=True)
        polled = self.m.clock()
        self.stats.polls += 1
        self.stats.last_poll_at = self.m.wall()
        self.stats.last_poll_ms = round((polled - started) * 1000, 1)

        if batch is None:
            if getattr(self.source, "finished", False):
                return "done"
            self.failures += 1
            self.stats.failures += 1
            if self.failures == CONSECUTIVE_FAILURES_BEFORE_DEGRADED:
                broadcaster.publish(self.key, DegradedEvent(type="degraded", reason="data source unavailable"))
            return "failed"

        self.failures = 0
        info = getattr(self.source, "last_poll", None)
        # Feed delay is only meaningful for plays seen as they happen, not for
        # history replayed on first connect or for a replay of a past game.
        live = info is not None and not info.backfill
        try:
            events = process_snapshots(self.state, batch, self.model, self.baseline, live=live, now=self.m.wall())
        except Exception:
            # A bug or bad data must not silently kill the poller (viewers would
            # get heartbeats forever): log it, count it as a failed poll and
            # carry on with the next one.
            logger.error("Engine error for %s", self.key, exc_info=True)
            self.failures += 1
            self.stats.failures += 1
            if self.failures == CONSECUTIVE_FAILURES_BEFORE_DEGRADED:
                broadcaster.publish(self.key, DegradedEvent(type="degraded", reason="data source unavailable"))
            return "failed"
        for event in events:
            broadcaster.publish(self.key, event)
        self.stats.last_publish_ms = round((self.m.clock() - polled) * 1000, 2)
        if info is not None:
            self.stats.last_fetch_ms = round(info.fetch_ms, 1)
            self.stats.last_feed_age_seconds = None if info.feed_age_seconds is None else round(info.feed_age_seconds, 2)
            self.stats.last_new_actions = info.new_actions
            self.stats.fidelity = info.fidelity
            if info.not_modified:
                self.stats.not_modified += 1
        if live:
            delays = [e.feed_delay_sec for e in events if getattr(e, "feed_delay_sec", None) is not None]
            if delays:
                self.stats.recent_feed_delays = (self.stats.recent_feed_delays + delays)[-20:]
        return "done" if any(s.game_status == "final" for s in batch) else "ok"


class PollerManager:
    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep, wall=time.time) -> None:
        self.clock = clock
        self.sleep = sleep
        self.wall = wall
        self._tasks: dict[str, asyncio.Task] = {}
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def is_active(self, key: str) -> bool:
        return key in self._tasks

    def active_keys(self) -> list[str]:
        return list(self._tasks.keys())

    def stats(self, key: str) -> dict | None:
        session = self._sessions.get(key)
        if session is None:
            return None
        return {**asdict(session.stats), "base_interval_seconds": session.base_interval, "active": key in self._tasks}

    def start(
        self,
        key: str,
        source: DataSource,
        model,
        baseline,
        interval_seconds: float,
        source_game_id: str,
        idle_stop_seconds: float | None = None,
    ) -> None:
        if key in self._tasks:
            return
        session = _Session(self, key, source, model, baseline, interval_seconds, source_game_id, idle_stop_seconds)
        self._remember(session)
        self._tasks[key] = asyncio.create_task(self._run(session, skip_first=False))

    async def start_primed(
        self,
        key: str,
        source: DataSource,
        model,
        baseline,
        interval_seconds: float,
        source_game_id: str,
        idle_stop_seconds: float | None = None,
    ) -> None:
        """Like start(), but the first poll runs before this returns.

        For a live game that first poll fetches the whole play-by-play so far
        and runs every play through the engine, so the state is complete before
        the first viewer's stream begins: a late viewer gets the full WP
        curve and every heat check that already happened. Raises
        SourceUnavailable if that first poll fails.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._tasks:
                return
            session = _Session(self, key, source, model, baseline, interval_seconds, source_game_id, idle_stop_seconds)
            self._remember(session)
            try:
                outcome = await session.poll_once()
            except BaseException:
                game_state_store.remove(key)  # don't leave an active-looking, never-retained state behind
                self._sessions.pop(key, None)
                raise
            if outcome == "failed":
                self._sessions.pop(key, None)
                self._locks.pop(key, None)
                game_state_store.remove(key)  # nothing was learned; don't keep an empty shell
                raise SourceUnavailable(f"Could not read game {source_game_id}")
            self._tasks[key] = asyncio.create_task(self._run(session, skip_first=True, finished=outcome == "done"))

    def _remember(self, session: _Session) -> None:
        self._sessions.pop(session.key, None)
        self._sessions[session.key] = session
        while len(self._sessions) > MAX_REMEMBERED_SESSIONS:  # stats only; keep the newest
            oldest = next(iter(self._sessions))
            if oldest in self._tasks:
                break
            self._sessions.pop(oldest)

    def stop(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _run(self, s: _Session, skip_first: bool, finished: bool = False) -> None:
        try:
            if finished:
                return
            cycle_start = self.clock()
            while True:
                if not skip_first:
                    if s.idle_expired():
                        break
                    outcome = await s.poll_once()
                    if outcome == "done":
                        break
                    if outcome == "failed":
                        backoff = min(s.base_interval * s.failures, settings.poll_backoff_max_seconds)
                        await self.sleep(backoff)
                        cycle_start = self.clock()
                        continue
                skip_first = False
                # Fixed cadence: the next cycle starts one interval after this
                # one STARTED; sleep only for what is left of it.
                next_at = cycle_start + s.next_interval()
                now = self.clock()
                if next_at < now:  # a slow fetch overran the interval: don't burst to catch up
                    next_at = now
                await self.sleep(next_at - now)
                cycle_start = next_at
        finally:
            self._tasks.pop(s.key, None)
            self._locks.pop(s.key, None)
            # Replay sessions are one-off and unique per viewer. A live game's
            # state stays (bounded) so late visitors still see the whole game.
            if s.key.startswith("replay:"):
                game_state_store.remove(s.key)
                self._sessions.pop(s.key, None)
            else:
                game_state_store.retain(s.key)


poller_manager = PollerManager()

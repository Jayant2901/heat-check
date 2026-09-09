"""Drives one tracked key's DataSource through the engine, at a fixed
cadence, publishing every event to the broadcaster.

The SAME code runs a live game (NbaLiveDataSource, one real poll per
poll_interval_seconds) and a replay session (ReplaySource, one fixture tick
per poll_interval_seconds/speed) -- both are just a DataSource (see
core/interfaces.py). "key" is the GameStateStore/broadcaster identity: for a
live game it's the real NBA game_id; for a replay session it's a namespaced
"replay:{game_id}:{session_id}" so multiple viewers can each run their own
session without colliding. "source_game_id" is what gets passed to
DataSource.next_snapshot() -- the real game_id for live, ignored by
ReplaySource but still the real game_id for clarity.
"""

import asyncio
import logging

from app.core.config import settings
from app.core.engine import process_tick
from app.core.events import DegradedEvent
from app.core.game_state import game_state_store
from app.core.interfaces import DataSource
from app.sse.broadcaster import broadcaster

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURES_BEFORE_DEGRADED = 3


class PollerManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def is_active(self, key: str) -> bool:
        return key in self._tasks

    def active_keys(self) -> list[str]:
        return list(self._tasks.keys())

    def start(
        self,
        key: str,
        source: DataSource,
        model,
        baseline,
        interval_seconds: float,
        source_game_id: str,
    ) -> None:
        if key in self._tasks:
            return
        self._tasks[key] = asyncio.create_task(
            self._run(key, source, model, baseline, interval_seconds, source_game_id)
        )

    def stop(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def _run(
        self,
        key: str,
        source: DataSource,
        model,
        baseline,
        interval_seconds: float,
        source_game_id: str,
    ) -> None:
        state = game_state_store.get_or_create(key)
        consecutive_failures = 0
        try:
            while True:
                try:
                    snapshot = await source.next_snapshot(source_game_id)
                except Exception:
                    snapshot = None
                    logger.warning("Data source error for %s", key, exc_info=True)

                if snapshot is None:
                    if getattr(source, "finished", False):
                        break
                    consecutive_failures += 1
                    if consecutive_failures == CONSECUTIVE_FAILURES_BEFORE_DEGRADED:
                        broadcaster.publish(
                            key, DegradedEvent(type="degraded", reason="data source unavailable")
                        )
                    backoff = min(interval_seconds * consecutive_failures, settings.poll_backoff_max_seconds)
                    await asyncio.sleep(backoff)
                    continue

                consecutive_failures = 0
                for event in process_tick(state, snapshot, model, baseline):
                    broadcaster.publish(key, event)

                if snapshot.game_status == "final":
                    break
                await asyncio.sleep(interval_seconds)
        finally:
            # Nothing else removes a finished game/session from the store --
            # without this, every game_end (live or replay) leaked its full
            # score_history/anomalies forever, unbounded, for the life of
            # the process. Any SSE connection already open keeps working
            # fine (event_generator holds its own reference to `state`);
            # the only effect is that a *new* connection to this same key
            # after it's gone either restarts a fresh live poll (harmless,
            # since a truly-finished game just re-completes) or 404s for a
            # finished replay session, which is fine -- start a new one.
            self._tasks.pop(key, None)
            game_state_store.remove(key)


poller_manager = PollerManager()

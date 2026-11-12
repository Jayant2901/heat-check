"""Steps a completed game's snapshot sequence through the DataSource protocol.

Speed control lives in the poller (backend/app/polling/poller.py), which
decides how often to poll -- this class's only job is to hand back the next
snapshot in a pre-built sequence each time it's asked, exactly like the live
sources hand back whatever the feed currently shows. That symmetry is what
lets core/engine.py run identically for both -- see
backend/tests/test_replay_source.py.

Curated fixtures (data/replay_fixtures) and past-game replays
(app/nba/replays.py) are both one snapshot per play-by-play action, so they
are marked exact and take the same engine path as live play-by-play.
"""

import json
from pathlib import Path

from app.core.interfaces import GameSnapshot


class ReplaySource:
    def __init__(self, fixture_path: Path | None = None, start_index: int = 0, snapshots: list[dict] | None = None):
        # Either a fixture file, or snapshot dicts built on the fly (a past
        # game's play-by-play, see app/nba/replays.py).
        if snapshots is None:
            snapshots = json.loads(Path(fixture_path).read_text())["snapshots"]
        self._snapshots = [GameSnapshot(**{"exact": True, **s}) for s in snapshots]
        self._index = max(0, min(start_index, len(self._snapshots) - 1))

    @property
    def finished(self) -> bool:
        return self._index >= len(self._snapshots)

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        if self.finished:
            return None
        snapshot = self._snapshots[self._index]
        self._index += 1
        return snapshot

    async def next_snapshots(self, game_id: str) -> list[GameSnapshot] | None:
        snapshot = await self.next_snapshot(game_id)
        return None if snapshot is None else [snapshot]

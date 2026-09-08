"""Steps a completed game's snapshot sequence through the DataSource protocol.

Speed control lives in the poller (backend/app/polling/poller.py), which
decides how often to call next_snapshot -- this class's only job is to hand
back the next snapshot in a pre-built sequence each time it's asked, exactly
like NbaLiveDataSource hands back whatever the live CDN currently shows.
That symmetry is what lets core/engine.py's process_tick run identically for
both -- see backend/tests/test_replay_source.py.
"""

import json
from pathlib import Path

from app.core.interfaces import GameSnapshot


class ReplaySource:
    def __init__(self, fixture_path: Path):
        data = json.loads(Path(fixture_path).read_text())
        self._snapshots = [GameSnapshot(**s) for s in data["snapshots"]]
        self._index = 0

    @property
    def finished(self) -> bool:
        return self._index >= len(self._snapshots)

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        if self.finished:
            return None
        snapshot = self._snapshots[self._index]
        self._index += 1
        return snapshot

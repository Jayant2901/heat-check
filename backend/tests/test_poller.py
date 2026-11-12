import asyncio
import json

import pandas as pd
import pytest

from app.core.game_state import game_state_store
from app.core.interfaces import GameSnapshot
from app.datasources.replay_source import ReplaySource
from app.polling.poller import PollerManager, SourceUnavailable
from app.sse.broadcaster import broadcaster

EMPTY_BASELINE = pd.DataFrame(
    [], columns=["period", "time_bucket", "magnitude", "mean_log_duration", "std_log_duration", "n"]
)


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]] * len(X)


def _snap(period, clock, home, away, status="live"):
    return {
        "game_id": "T1",
        "period": period,
        "game_clock_seconds_remaining": clock,
        "home_team": "HOM",
        "away_team": "AWY",
        "home_score": home,
        "away_score": away,
        "game_status": status,
    }


def _write_fixture(tmp_path, snapshots):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"game_id": "T1", "snapshots": snapshots}))
    return path


@pytest.mark.asyncio
async def test_poller_publishes_events_and_stops_at_game_end(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(1, 700, 2, 0), _snap(4, 0, 100, 90, status="final")]
    fixture_path = _write_fixture(tmp_path, snapshots)

    manager = PollerManager()
    queue = broadcaster.subscribe("T1")
    try:
        manager.start("T1", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 0.01, source_game_id="T1")

        wp_events = [await asyncio.wait_for(queue.get(), timeout=2.0) for _ in range(3)]
        assert [e.type for e in wp_events] == ["wp_update", "wp_update", "wp_update"]
        assert wp_events[-1].home_score == 100

        game_end = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert game_end.type == "game_end"

        # the task cleans itself up once the source is exhausted
        for _ in range(50):
            if not manager.is_active("T1"):
                break
            await asyncio.sleep(0.01)
        assert not manager.is_active("T1")
        # ...but a finished game's state is KEPT (bounded, see test_game_state.py)
        # so a visitor arriving later still sees the whole game.
        kept = game_state_store.get("T1")
        assert kept is not None and kept.status == "final" and len(kept.score_history) == 3
        assert game_state_store.is_retained("T1")
    finally:
        broadcaster.unsubscribe("T1", queue)
        game_state_store.remove("T1")


@pytest.mark.asyncio
async def test_starting_an_already_tracked_key_is_a_no_op(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(4, 0, 10, 5, status="final")]
    fixture_path = _write_fixture(tmp_path, snapshots)
    manager = PollerManager()

    try:
        manager.start("T2", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 5.0, source_game_id="T2")
        first_task = manager._tasks["T2"]
        manager.start("T2", ReplaySource(fixture_path), FakeModel(), EMPTY_BASELINE, 5.0, source_game_id="T2")

        assert manager._tasks["T2"] is first_task
    finally:
        manager.stop("T2")
        game_state_store.remove("T2")


class _ForeverLive:
    """A live source that never finishes, like a game in progress."""

    def __init__(self):
        self.calls = 0

    async def next_snapshots(self, game_id):
        self.calls += 1
        return [
            GameSnapshot(
                game_id=game_id, period=2, game_clock_seconds_remaining=300.0, home_team="HOM",
                away_team="AWY", home_score=50, away_score=48, game_status="live",
            )
        ]


@pytest.mark.asyncio
async def test_idle_live_poller_stops_itself():
    manager = PollerManager()
    try:
        manager.start("IDLE1", _ForeverLive(), FakeModel(), EMPTY_BASELINE, 0.01, "IDLE1", idle_stop_seconds=0.05)
        for _ in range(100):
            if not manager.is_active("IDLE1"):
                break
            await asyncio.sleep(0.01)
        assert not manager.is_active("IDLE1")
        assert game_state_store.is_retained("IDLE1")  # bounded cache, not deleted
    finally:
        manager.stop("IDLE1")
        game_state_store.remove("IDLE1")


@pytest.mark.asyncio
async def test_watched_live_poller_keeps_running():
    manager = PollerManager()
    source = _ForeverLive()
    queue = broadcaster.subscribe("WATCHED1")
    try:
        manager.start("WATCHED1", source, FakeModel(), EMPTY_BASELINE, 0.01, "WATCHED1", idle_stop_seconds=0.05)
        await asyncio.sleep(0.25)  # 5x the idle limit
        assert manager.is_active("WATCHED1")
        assert source.calls > 5
    finally:
        broadcaster.unsubscribe("WATCHED1", queue)
        manager.stop("WATCHED1")
        game_state_store.remove("WATCHED1")


class FakeClock:
    """Time that only moves when something sleeps or 'fetches'."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


class _TimedSource:
    """Each poll 'takes' fetch_seconds of fake time and records when it began."""

    def __init__(self, clock, fetch_seconds, polls, interval=None):
        self.clock = clock
        self.fetch_seconds = fetch_seconds
        self.remaining = polls
        self.started = []
        self.interval = interval

    async def next_snapshots(self, game_id):
        self.started.append(self.clock.t)
        self.clock.t += self.fetch_seconds
        self.remaining -= 1
        status = "final" if self.remaining == 0 else "live"
        return [GameSnapshot(game_id, 2, 300.0, "HOM", "AWY", 50, 48, status)]

    def suggest_interval(self, base):
        return base if self.interval is None else self.interval(len(self.started), base)


async def _until_inactive(manager, key):
    for _ in range(2000):
        if not manager.is_active(key):
            return
        await asyncio.sleep(0)
    raise AssertionError("poller did not finish")


@pytest.mark.asyncio
async def test_poll_cadence_does_not_drift_by_fetch_time():
    """The old loop was fetch-then-sleep(interval): with a 0.8 s fetch and a 3 s
    interval it polled every 3.8 s. Scheduling against the cycle's start keeps
    it at 3.0 s."""
    clock = FakeClock()
    manager = PollerManager(clock=clock.now, sleep=clock.sleep, wall=clock.now)
    source = _TimedSource(clock, fetch_seconds=0.8, polls=6)
    try:
        manager.start("CAD1", source, FakeModel(), EMPTY_BASELINE, 3.0, "CAD1")
        await _until_inactive(manager, "CAD1")
        assert source.started == pytest.approx([0.0, 3.0, 6.0, 9.0, 12.0, 15.0])
    finally:
        manager.stop("CAD1")
        game_state_store.remove("CAD1")


@pytest.mark.asyncio
async def test_a_fetch_longer_than_the_interval_does_not_cause_a_burst():
    clock = FakeClock()
    manager = PollerManager(clock=clock.now, sleep=clock.sleep, wall=clock.now)
    source = _TimedSource(clock, fetch_seconds=5.0, polls=4)  # slower than the 3 s interval
    try:
        manager.start("CAD2", source, FakeModel(), EMPTY_BASELINE, 3.0, "CAD2")
        await _until_inactive(manager, "CAD2")
        gaps = [b - a for a, b in zip(source.started, source.started[1:])]
        assert gaps == pytest.approx([5.0, 5.0, 5.0])  # as fast as it can go, never faster
    finally:
        manager.stop("CAD2")
        game_state_store.remove("CAD2")


@pytest.mark.asyncio
async def test_the_source_can_slow_and_speed_the_cadence():
    clock = FakeClock()
    manager = PollerManager(clock=clock.now, sleep=clock.sleep, wall=clock.now)
    # polls 1-2 at the base 3 s; the feed goes quiet (15 s) for polls 3-4, then resumes
    source = _TimedSource(clock, 0.0, polls=6, interval=lambda n, base: 15.0 if n in (3, 4) else base)
    try:
        manager.start("CAD3", source, FakeModel(), EMPTY_BASELINE, 3.0, "CAD3")
        await _until_inactive(manager, "CAD3")
        gaps = [b - a for a, b in zip(source.started, source.started[1:])]
        assert gaps == pytest.approx([3.0, 3.0, 15.0, 15.0, 3.0])
    finally:
        manager.stop("CAD3")
        game_state_store.remove("CAD3")


class _Flaky:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    async def next_snapshots(self, game_id):
        self.calls += 1
        if self.calls <= self.failures:
            return None
        return [GameSnapshot(game_id, 4, 0.0, "HOM", "AWY", 90, 88, "final")]


@pytest.mark.asyncio
async def test_repeated_failures_publish_degraded_once_and_back_off():
    clock = FakeClock()
    manager = PollerManager(clock=clock.now, sleep=clock.sleep, wall=clock.now)
    queue = broadcaster.subscribe("FLAKY")
    try:
        manager.start("FLAKY", _Flaky(failures=4), FakeModel(), EMPTY_BASELINE, 3.0, "FLAKY")
        await _until_inactive(manager, "FLAKY")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert [e.type for e in events].count("degraded") == 1
        assert [e.type for e in events][-2:] == ["wp_update", "game_end"]
        assert clock.t == pytest.approx(3 + 6 + 9 + 12)  # backoff grows with each failure
    finally:
        broadcaster.unsubscribe("FLAKY", queue)
        manager.stop("FLAKY")
        game_state_store.remove("FLAKY")


class _Backfiller:
    backfills = True

    def __init__(self, batches):
        self.batches = list(batches)

    async def next_snapshots(self, game_id):
        return self.batches.pop(0) if self.batches else []


def _live(n, home, away, status="live"):
    return GameSnapshot("G", 1, 700.0 - n, "HOM", "AWY", home, away, status, exact=True, action_number=n)


@pytest.mark.asyncio
async def test_start_primed_runs_the_first_poll_before_returning():
    manager = PollerManager()
    source = _Backfiller([[_live(n, 2 * n, 0) for n in range(1, 31)], []])
    try:
        await manager.start_primed("PRIME1", source, FakeModel(), EMPTY_BASELINE, 30.0, "PRIME1")
        state = game_state_store.get("PRIME1")
        assert len(state.score_history) == 30  # already complete: no waiting for the loop
        assert manager.is_active("PRIME1")
        assert manager.stats("PRIME1")["polls"] == 1
    finally:
        manager.stop("PRIME1")
        game_state_store.remove("PRIME1")


@pytest.mark.asyncio
async def test_start_primed_raises_and_leaves_nothing_behind_when_the_first_poll_fails():
    manager = PollerManager()
    with pytest.raises(SourceUnavailable):
        await manager.start_primed("PRIME2", _Flaky(failures=99), FakeModel(), EMPTY_BASELINE, 30.0, "PRIME2")
    assert not manager.is_active("PRIME2")
    assert game_state_store.get("PRIME2") is None


@pytest.mark.asyncio
async def test_a_game_that_is_already_over_is_primed_and_kept_without_a_running_poller():
    manager = PollerManager()
    source = _Backfiller([[_live(1, 0, 0), _live(2, 90, 88, status="final")]])
    await manager.start_primed("PRIME3", source, FakeModel(), EMPTY_BASELINE, 30.0, "PRIME3")
    await _until_inactive(manager, "PRIME3")
    assert game_state_store.get("PRIME3").status == "final"
    assert game_state_store.is_retained("PRIME3")
    game_state_store.remove("PRIME3")


@pytest.mark.asyncio
async def test_a_backfilling_poller_replaces_stale_state_instead_of_stacking_on_it():
    """An idle poller stopped, its half-built state was kept, a new viewer
    starts a new poller. The new poller re-reads the whole game, so the old
    copy must not be underneath."""
    manager = PollerManager()
    stale = game_state_store.get_or_create("PRIME4")
    stale.home_score = 10
    stale.score_history.append(object())
    game_state_store.retain("PRIME4")
    source = _Backfiller([[_live(n, 2 * n, 0) for n in range(1, 6)]])
    try:
        await manager.start_primed("PRIME4", source, FakeModel(), EMPTY_BASELINE, 30.0, "PRIME4")
        state = game_state_store.get("PRIME4")
        assert state is not stale and len(state.score_history) == 5
        assert not game_state_store.is_retained("PRIME4")  # active again, so not evictable
    finally:
        manager.stop("PRIME4")
        game_state_store.remove("PRIME4")


@pytest.mark.asyncio
async def test_replay_sessions_are_removed_when_they_finish_but_live_games_are_kept(tmp_path):
    snapshots = [_snap(1, 720, 0, 0), _snap(4, 0, 10, 5, status="final")]
    manager = PollerManager()
    manager.start("replay:T:abc", ReplaySource(_write_fixture(tmp_path, snapshots)), FakeModel(), EMPTY_BASELINE, 0.001, "T")
    await _until_inactive(manager, "replay:T:abc")
    assert game_state_store.get("replay:T:abc") is None
    assert manager.stats("replay:T:abc") is None


@pytest.mark.asyncio
async def test_stats_report_the_polls():
    clock = FakeClock()
    manager = PollerManager(clock=clock.now, sleep=clock.sleep, wall=clock.now)
    try:
        manager.start("STAT1", _TimedSource(clock, 0.5, polls=3), FakeModel(), EMPTY_BASELINE, 3.0, "STAT1")
        await _until_inactive(manager, "STAT1")
        stats = manager.stats("STAT1")
        assert stats["polls"] == 3 and stats["failures"] == 0
        assert stats["last_poll_ms"] == pytest.approx(500.0)
        assert stats["base_interval_seconds"] == 3.0 and stats["active"] is False
        assert stats["last_publish_ms"] is not None
    finally:
        game_state_store.remove("STAT1")

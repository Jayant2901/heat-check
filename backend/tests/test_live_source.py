"""PlayByPlayLiveSource: play-by-play -> exact snapshots.

Two kinds of payload are used:
* ESPN: a REAL captured game (fixtures/espn_summary_401909968.json) through
  the app's own ESPN translator.
* NBA.com: built here in the shape of the sample payload shipped in
  nba_api/live/nba/endpoints/playbyplay.py (actionNumber, clock "PT11M58.00S",
  timeActual, scoreHome/scoreAway as strings). No real cdn.nba.com response
  could be captured (the host answers 403 from the build environment), so the
  NBA field names are verified against nba_api's sample only.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from app.core.engine import process_snapshots
from app.core.game_state import GameState
from app.core.interfaces import FIDELITY_BOXSCORE_TICKS, FIDELITY_PLAY_BY_PLAY
from app.datasources.nba_live import PlayByPlayLiveSource, action_kind, parse_timestamp
from app.nba import espn
from app.nba.client import FeedResult, NbaUnavailable

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_BASELINE = pd.DataFrame(
    [], columns=["period", "time_bucket", "magnitude", "mean_log_duration", "std_log_duration", "n"]
)


class FakeModel:
    def predict_proba(self, X):
        return [[0.5, 0.5]] * len(X)


def nba_action(number, period, clock, home, away, kind="2pt", ts="2026-11-12T00:40:31.3Z", sub=""):
    """An action in the shape of nba_api's sample payload."""
    return {
        "actionNumber": number,
        "clock": clock,
        "timeActual": ts,
        "period": period,
        "teamTricode": "BOS",
        "actionType": kind,
        "subType": sub,
        "scoreHome": str(home),
        "scoreAway": str(away),
        "description": "x",
    }


def box(status, home_score=0, away_score=0):
    return {
        "game": {
            "gameId": "0022600001",
            "gameStatus": status,
            "period": 1,
            "gameClock": "PT10M00.00S",
            "homeTeam": {"teamTricode": "BOS", "score": home_score},
            "awayTeam": {"teamTricode": "NYK", "score": away_score},
        }
    }


class Feeds:
    """Scripted pbp / boxscore responses; an Exception in the script is raised."""

    def __init__(self, pbp, boxes):
        self.pbp = list(pbp)
        self.boxes = list(boxes)
        self.pbp_calls = self.box_calls = 0

    @staticmethod
    def _next(script, calls):
        item = script[min(calls, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_pbp(self, game_id):
        item = self._next(self.pbp, self.pbp_calls)
        self.pbp_calls += 1
        return item

    async def fetch_box(self, game_id):
        item = self._next(self.boxes, self.box_calls)
        self.box_calls += 1
        return item


def pbp(*actions):
    return {"game": {"gameId": "0022600001", "actions": list(actions)}}


def source(feeds, clock=lambda: 0.0, monotonic=None, **kwargs):
    extra = {} if monotonic is None else {"monotonic": monotonic}
    return PlayByPlayLiveSource(feeds.fetch_pbp, feeds.fetch_box, home="BOS", away="NYK", clock=clock, **extra, **kwargs)


@pytest.fixture(autouse=True)
def zero_box_refresh(monkeypatch):
    # refresh the boxscore on every poll in these tests
    monkeypatch.setattr("app.datasources.nba_live.settings", type("S", (), {
        "boxscore_refresh_seconds": 0.0, "live_idle_poll_seconds": 10.0, "live_deep_idle_poll_seconds": 15.0})())


def test_parse_timestamp():
    assert parse_timestamp("2021-01-16T00:40:31.3Z") == pytest.approx(1610757631.3)
    assert parse_timestamp("2026-11-12T00:10:24Z") is not None
    assert parse_timestamp("") is None and parse_timestamp(None) is None and parse_timestamp("junk") is None


def test_action_kind_reads_both_feeds():
    assert action_kind({"actionType": "game", "subType": "end"}) == "game_end"
    assert action_kind({"actionType": "End Game", "description": "End of Game"}) == "game_end"
    assert action_kind({"actionType": "period", "subType": "end"}) == "period_end"
    assert action_kind({"actionType": "2pt", "subType": "Layup"}) == ""


@pytest.mark.asyncio
async def test_first_poll_returns_every_play_so_far_as_exact_snapshots():
    actions = [
        nba_action(2, 1, "PT12M00.00S", 0, 0, "period", ts="2026-11-12T00:00:00Z"),
        nba_action(3, 1, "PT11M40.00S", 2, 0, ts="2026-11-12T00:00:20Z"),
        nba_action(4, 1, "PT11M40.00S", 2, 0, "foul"),  # same moment, same score: adds nothing
        nba_action(5, 1, "PT11M20.00S", 2, 3, ts="2026-11-12T00:00:40Z"),
    ]
    feeds = Feeds([pbp(*actions)], [box(2, 2, 3)])
    src = source(feeds)
    snaps = await src.next_snapshots("0022600001")
    assert [(s.period, s.game_clock_seconds_remaining, s.home_score, s.away_score) for s in snaps] == [
        (1, 720.0, 0, 0),
        (1, 700.0, 2, 0),
        (1, 680.0, 2, 3),
    ]
    assert all(s.exact and s.game_status == "live" for s in snaps)
    assert [s.action_number for s in snaps] == [1, 2, 4]  # list positions: see _actions()
    assert snaps[1].source_ts == parse_timestamp("2026-11-12T00:00:20Z")
    assert (snaps[0].home_team, snaps[0].away_team) == ("BOS", "NYK")
    assert src.last_poll.backfill is True and src.last_poll.new_actions == 4


@pytest.mark.asyncio
async def test_later_polls_return_only_new_plays_and_a_304_returns_nothing():
    first = [nba_action(1, 1, "PT12M00.00S", 0, 0), nba_action(2, 1, "PT11M50.00S", 2, 0)]
    more = first + [nba_action(3, 1, "PT11M30.00S", 2, 2)]
    unchanged = FeedResult(pbp(*more), not_modified=True)
    feeds = Feeds([pbp(*first), pbp(*more), unchanged], [box(2)])
    src = source(feeds)
    assert len(await src.next_snapshots("g")) == 2
    second = await src.next_snapshots("g")
    assert [s.action_number for s in second] == [3]
    assert src.last_poll.backfill is False
    assert await src.next_snapshots("g") == []
    assert src.last_poll.not_modified is True


@pytest.mark.asyncio
async def test_final_status_marks_the_last_play_final():
    actions = [nba_action(1, 4, "PT00M10.00S", 98, 97), nba_action(2, 4, "PT00M00.00S", 100, 97)]
    feeds = Feeds([pbp(*actions)], [box(3, 100, 97)])
    snaps = await source(feeds).next_snapshots("g")
    assert [s.game_status for s in snaps] == ["live", "final"]


@pytest.mark.asyncio
async def test_a_game_end_play_is_final_even_if_the_boxscore_lags():
    actions = [nba_action(1, 4, "PT00M05.00S", 98, 97), nba_action(2, 4, "PT00M00.00S", 98, 97, "game", sub="end")]
    feeds = Feeds([pbp(*actions)], [box(2, 98, 97)])  # the boxscore still says live
    snaps = await source(feeds).next_snapshots("g")
    assert snaps[-1].game_status == "final"


@pytest.mark.asyncio
async def test_final_is_emitted_once_when_the_boxscore_catches_up_later():
    actions = [nba_action(1, 4, "PT00M05.00S", 98, 97)]
    feeds = Feeds([pbp(*actions)], [box(2), box(3, 98, 97)])
    src = source(feeds)
    assert [s.game_status for s in await src.next_snapshots("g")] == ["live"]
    final = await src.next_snapshots("g")
    assert [s.game_status for s in final] == ["final"]
    assert (final[0].home_score, final[0].away_score) == (98, 97)
    assert await src.next_snapshots("g") == []  # not repeated


@pytest.mark.asyncio
async def test_before_tipoff_a_missing_feed_is_not_a_failure():
    not_yet = NbaUnavailable("404", "HTTP 404")
    feeds = Feeds([not_yet], [box(1)])
    src = source(feeds)
    first = await src.next_snapshots("g")
    assert [(s.game_status, s.period) for s in first] == [("scheduled", 0)]
    for _ in range(6):  # many polls: still scheduled, never falls back to ticks
        assert await src.next_snapshots("g") == []
    assert src.fidelity == FIDELITY_PLAY_BY_PLAY


@pytest.mark.asyncio
async def test_a_refused_feed_falls_back_to_ticks_on_the_very_first_poll():
    """A 403 will not clear in a few seconds, and a game's first connection
    builds a fresh source with no failure count, so waiting for three failures
    would mean a 503 for every first viewer on a host the CDN blocks."""
    feeds = Feeds([NbaUnavailable("403", "HTTP 403")], [box(2, 10, 8)])
    src = source(feeds)
    snaps = await src.next_snapshots("g")
    assert src.fidelity == FIDELITY_BOXSCORE_TICKS
    assert len(snaps) == 1 and snaps[0].exact is False and (snaps[0].home_score, snaps[0].away_score) == (10, 8)
    assert src.last_poll.fidelity == FIDELITY_BOXSCORE_TICKS
    feeds.boxes = [box(2, 12, 8)]
    assert (await src.next_snapshots("g"))[0].home_score == 12


@pytest.mark.asyncio
async def test_other_failures_fall_back_after_three_in_a_row():
    feeds = Feeds([NbaUnavailable("503", "HTTP 503")], [box(2, 10, 8)])
    src = source(feeds)
    assert await src.next_snapshots("g") is None
    assert await src.next_snapshots("g") is None
    assert src.fidelity == FIDELITY_PLAY_BY_PLAY
    third = await src.next_snapshots("g")
    assert src.fidelity == FIDELITY_BOXSCORE_TICKS and third[0].exact is False


@pytest.mark.asyncio
async def test_a_404_on_a_live_game_gets_a_longer_grace_than_other_failures():
    """The CDN can lag tip-off by a while; falling back (permanently, for the
    life of the poller) on three 404s would degrade a game for no reason."""
    feeds = Feeds([NbaUnavailable("404", "HTTP 404")], [box(2, 10, 8)])
    src = source(feeds)
    for _ in range(9):
        assert await src.next_snapshots("g") is None
    assert src.fidelity == FIDELITY_PLAY_BY_PLAY
    tenth = await src.next_snapshots("g")
    assert src.fidelity == FIDELITY_BOXSCORE_TICKS and tenth[0].exact is False


@pytest.mark.asyncio
async def test_an_empty_feed_for_a_game_that_has_scored_falls_back_too():
    feeds = Feeds([pbp()], [box(2, 10, 8)])
    src = source(feeds)
    assert await src.next_snapshots("g") is None
    assert await src.next_snapshots("g") is None
    snaps = await src.next_snapshots("g")
    assert src.fidelity == FIDELITY_BOXSCORE_TICKS and snaps[0].exact is False


@pytest.mark.asyncio
async def test_poll_interval_eases_off_when_the_feed_goes_quiet():
    now = {"t": 0.0}
    feeds = Feeds([pbp(nba_action(1, 2, "PT05M00.00S", 40, 38))], [box(2, 40, 38)])
    src = source(feeds, monotonic=lambda: now["t"])
    await src.next_snapshots("g")
    assert src.suggest_interval(3.0) == 3.0  # plays just arrived
    now["t"] = 50.0
    assert src.suggest_interval(3.0) == 10.0  # quiet for 50 s
    now["t"] = 200.0
    assert src.suggest_interval(3.0) == 15.0  # halftime-length silence
    feeds.pbp = [pbp(nba_action(1, 2, "PT05M00.00S", 40, 38), nba_action(2, 2, "PT04M50.00S", 42, 38))]
    feeds.pbp_calls = 0
    await src.next_snapshots("g")
    assert src.suggest_interval(3.0) == 3.0  # play resumed: back to the fast cadence


@pytest.mark.asyncio
async def test_a_period_end_play_slows_polling_immediately():
    feeds = Feeds([pbp(nba_action(1, 2, "PT00M00.00S", 50, 48, "period", sub="end"))], [box(2, 50, 48)])
    src = source(feeds)
    await src.next_snapshots("g")
    assert src.suggest_interval(3.0) == 10.0


def _real_espn_feed():
    summary = json.loads((FIXTURES / "espn_summary_401909968.json").read_text())
    return summary, espn.translate_playbyplay(summary, "401909968")


@pytest.mark.asyncio
async def test_a_real_espn_game_replays_to_its_real_final_score():
    """Real captured data end to end: ESPN summary -> translator -> live source -> engine."""
    summary, feed = _real_espn_feed()
    boxscore = espn.translate_boxscore(summary, "401909968")
    feeds = Feeds([feed], [boxscore])
    src = source(feeds)
    snaps = await src.next_snapshots("401909968")
    assert snaps[-1].game_status == "final"
    assert (snaps[-1].home_team, snaps[-1].away_team) == ("TOR", "BOS")
    assert (snaps[-1].home_score, snaps[-1].away_score) == (93, 98)
    assert all(s.exact for s in snaps)
    # ESPN's plays carry their own timestamps
    assert snaps[5].source_ts is not None

    from app.core.artifacts import get_baseline, get_model

    state = GameState(game_id="401909968")
    process_snapshots(state, snaps, get_model(), get_baseline(), now=0.0)
    assert (state.home_score, state.away_score) == (93, 98) and state.status == "final"
    assert len(state.score_history) == len(snaps)
    assert [e.type for e in state.log].count("game_end") == 1
    assert state.log[-1].type == "game_end"


@pytest.mark.asyncio
async def test_building_a_real_game_in_chunks_equals_building_it_at_once():
    """Late joiner (everything in one first poll) vs a viewer who was there
    (the feed growing between polls): the same state, event for event."""
    summary, feed = _real_espn_feed()
    boxscore = espn.translate_boxscore(summary, "401909968")
    actions = feed["game"]["actions"]

    from app.core.artifacts import get_baseline, get_model

    model, baseline = get_model(), get_baseline()

    once = source(Feeds([feed], [boxscore]))
    late = GameState(game_id="late")
    process_snapshots(late, await once.next_snapshots("g"), model, baseline, now=1.0)

    growing_script = [{"game": {"actions": actions[:n]}} for n in range(40, len(actions), 40)] + [feed]
    in_progress = {"game": {**boxscore["game"], "gameStatus": 2}}
    feeds = Feeds(growing_script, [in_progress] * (len(growing_script) - 1) + [boxscore])
    live = source(feeds)
    streamed = GameState(game_id="live")
    for _ in growing_script:
        process_snapshots(streamed, await live.next_snapshots("g"), model, baseline, now=1.0)

    assert late.log == streamed.log
    assert late.anomalies == streamed.anomalies and len(late.anomalies) >= 1
    assert late.score_history == streamed.score_history


@pytest.mark.asyncio
async def test_final_waits_for_the_plays_to_catch_up_with_the_boxscore():
    """The boxscore can say final seconds before the last baskets reach the
    play-by-play file. The game must not end with the score short."""
    behind = pbp(nba_action(1, 4, "PT00M30.00S", 95, 97))
    caught_up = pbp(nba_action(1, 4, "PT00M30.00S", 95, 97), nba_action(2, 4, "PT00M00.00S", 98, 97))
    feeds = Feeds([behind, behind, caught_up], [box(3, 98, 97)])
    src = source(feeds)
    assert [s.game_status for s in await src.next_snapshots("g")] == ["live"]
    assert await src.next_snapshots("g") == []  # still waiting
    final = await src.next_snapshots("g")
    assert [(s.home_score, s.game_status) for s in final] == [(98, "final")]


@pytest.mark.asyncio
async def test_final_is_not_held_forever_if_the_scores_never_agree():
    feeds = Feeds([pbp(nba_action(1, 4, "PT00M30.00S", 95, 97))], [box(3, 99, 97)])
    src = source(feeds)
    statuses = []
    for _ in range(14):
        statuses += [s.game_status for s in await src.next_snapshots("g")]
    assert statuses[-1] == "final"


@pytest.mark.asyncio
async def test_plays_are_read_in_list_order_even_when_the_feeds_own_numbers_are_not_increasing():
    """ESPN's real sequenceNumber jumps around along its (chronological) list,
    so 'new since last poll' must go by position. Numbers here are out of order."""
    first = [nba_action(900, 1, "PT12M00.00S", 0, 0), nba_action(50, 1, "PT11M50.00S", 2, 0)]
    more = first + [nba_action(7, 1, "PT11M30.00S", 2, 2), nba_action(1000, 1, "PT11M10.00S", 5, 2)]
    feeds = Feeds([pbp(*first), pbp(*more)], [box(2)])
    src = source(feeds)
    assert [s.home_score for s in await src.next_snapshots("g")] == [0, 2]
    assert [(s.home_score, s.away_score) for s in await src.next_snapshots("g")] == [(2, 2), (5, 2)]


def test_the_captured_espn_feed_really_is_not_sorted_by_its_own_sequence_numbers():
    """Documents why the above matters, on real data."""
    summary, _ = _real_espn_feed()
    numbers = [int(p["sequenceNumber"]) for p in summary["plays"]]
    assert numbers != sorted(numbers)

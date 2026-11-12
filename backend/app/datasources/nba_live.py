"""Live DataSources: NBA.com's (or ESPN's, translated) feeds -> GameSnapshots.

Isolated behind the same DataSource protocol as the replay simulator
(app/datasources/replay_source.py) -- see app/core/interfaces.py. The
fetches are injected (normally HttpNbaClient.playbyplay_feed / boxscore_feed,
see app/nba/client.py), so this module only knows the payload shapes.

PlayByPlayLiveSource is the normal path. Each poll fetches the play-by-play
(conditionally, so an unchanged feed is a 304) and turns every action it has
not seen before into one exact snapshot, in order. The first poll of a game
returns the whole play-by-play so far, which is how a late viewer gets the
full chart and every past heat check. The boxscore is read only every few
seconds, for game status.

If play-by-play cannot be had (the CDN refuses it, repeatedly fails, or the feed
is empty for a game that has scored) the source falls back to boxscore polls and says so:
its snapshots are then exact=False and the events carry
fidelity="boxscore_ticks". It does not switch back mid-game, because the
state is already built from ticks.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime

from app.core.config import settings
from app.core.interfaces import FIDELITY_BOXSCORE_TICKS, FIDELITY_PLAY_BY_PLAY, GameSnapshot, PollInfo
from app.nba.client import FeedResult
from app.nba.parse import STATUS_BY_CODE, parse_clock_seconds

logger = logging.getLogger(__name__)

PBP_FAILURES_BEFORE_TICKS = 3
# A 404 for a live game's play-by-play usually means the CDN has not written the
# file yet (it can lag tip-off), so it gets a longer grace before falling back.
PBP_404_FAILURES_BEFORE_TICKS = 10
# The boxscore can say "final" a few seconds before the last plays reach the
# play-by-play file. Wait this many polls for the plays' score to match.
FINAL_WAIT_POLLS = 10
# No new play for this long: the clock is stopped for a while (timeout,
# halftime, a replay review), so poll less often until play resumes.
QUIET_SECONDS = 45.0
VERY_QUIET_SECONDS = 120.0


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _score(value) -> int | None:
    text = str(value if value is not None else "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def parse_timestamp(value) -> float | None:
    """'2021-01-16T00:40:31.3Z' -> epoch seconds; None if absent or junk."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def snapshot_from_boxscore(game: dict) -> GameSnapshot:
    """`game` is the boxscore payload's top-level "game" object."""
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    return GameSnapshot(
        game_id=game.get("gameId", ""),
        period=_int(game.get("period")),
        game_clock_seconds_remaining=parse_clock_seconds(game.get("gameClock")),
        home_team=home.get("teamTricode", ""),
        away_team=away.get("teamTricode", ""),
        home_score=_int(home.get("score")),
        away_score=_int(away.get("score")),
        game_status=STATUS_BY_CODE.get(_int(game.get("gameStatus")) or 1, "scheduled"),
    )


def action_kind(action: dict) -> str:
    """'game_end', 'period_end' or '' for an NBA.com or (translated) ESPN action."""
    kind = str(action.get("actionType") or "").lower()
    sub = str(action.get("subType") or "").lower()
    text = f"{kind} {sub} {str(action.get('description') or '').lower()}"
    if (kind == "game" and sub == "end") or "end game" in text or "game end" in text:
        return "game_end"
    if (kind == "period" and sub == "end") or "end period" in text or "end of the" in text:
        return "period_end"
    return ""


def _as_feed(result) -> FeedResult:
    return result if isinstance(result, FeedResult) else FeedResult(result)


def _actions(payload: dict) -> list[dict]:
    """The feed's actions in the order the feed lists them, each given
    actionNumber = its 1-based position.

    Position, not the feed's own number, is what "already seen" is based on:
    both feeds are append-only lists in game order, but ESPN's
    sequenceNumber is NOT increasing along that list (seen in a real
    capture), so trusting it would silently skip plays. Corrections to a
    play already read are not picked up.
    """
    raw = ((payload or {}).get("game") or {}).get("actions") or []
    return [{**action, "actionNumber": index + 1} for index, action in enumerate(a for a in raw if isinstance(a, dict))]


class PlayByPlayLiveSource:
    """One snapshot per new play-by-play action, falling back to boxscore ticks."""

    backfills = True  # a new instance replays the whole game from action 1

    def __init__(
        self,
        fetch_pbp: Callable[[str], Awaitable],
        fetch_box: Callable[[str], Awaitable],
        home: str = "",
        away: str = "",
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch_pbp = fetch_pbp
        self._fetch_box = fetch_box
        self._home = home
        self._away = away
        self._clock = clock
        self._monotonic = monotonic
        self.fidelity = FIDELITY_PLAY_BY_PLAY
        self.last_poll = PollInfo()
        self._first = True
        self._last_action = -1
        self._home_score = 0
        self._away_score = 0
        self._last_snapshot: GameSnapshot | None = None
        self._final_emitted = False
        self._pregame_emitted = False
        self._status = "scheduled"
        self._box_at = float("-inf")
        self._box_scores = (0, 0)
        self._pbp_failures = 0
        self._ticks = False
        self._changed_at = monotonic()
        self._period_ended = False
        self._final_waits = 0
        self._game_end_seen = False

    # -- boxscore (status, team codes) -----------------------------------

    async def _refresh_box(self, game_id: str, force: bool = False) -> bool:
        if not force and self._monotonic() - self._box_at < settings.boxscore_refresh_seconds:
            return True
        try:
            game = (_as_feed(await self._fetch_box(game_id)).data or {}).get("game") or {}
        except Exception:
            logger.warning("Boxscore fetch failed for %s", game_id, exc_info=True)
            return False
        self._box_at = self._monotonic()
        home, away = game.get("homeTeam") or {}, game.get("awayTeam") or {}
        self._home = home.get("teamTricode") or self._home
        self._away = away.get("teamTricode") or self._away
        self._status = STATUS_BY_CODE.get(_int(game.get("gameStatus")) or 1, "scheduled")
        self._box_scores = (_int(home.get("score")), _int(away.get("score")))
        return True

    # -- the protocol ------------------------------------------------------

    async def next_snapshots(self, game_id: str) -> list[GameSnapshot] | None:
        if self._ticks:
            return await self._tick_poll(game_id)
        try:
            feed = _as_feed(await self._fetch_pbp(game_id))
        except Exception as exc:
            return await self._pbp_failed(game_id, exc)
        actions = _actions(feed.data)

        # Status and team codes come from the boxscore: always on the first
        # poll and while the game has no plays yet, otherwise every few seconds.
        box_ok = await self._refresh_box(game_id, force=self._first or not actions)
        if self._first and not box_ok and not (self._home and self._away):
            return None  # no team codes yet; try again next tick

        if not actions:
            return await self._no_actions(game_id)
        self._pbp_failures = 0

        backfill = self._first
        fresh = [a for a in actions if a["actionNumber"] > self._last_action]
        snapshots = self._snapshots_for(game_id, fresh)
        if fresh:
            self._changed_at = self._monotonic()
            kind = action_kind(fresh[-1])
            self._period_ended = kind == "period_end"
            if kind == "game_end":
                # Definitive, whether or not the boxscore has caught up yet.
                self._game_end_seen = True
                await self._refresh_box(game_id, force=True)
                self._status = "final"
        snapshots = self._apply_final(game_id, snapshots)
        self._first = False

        newest_ts = parse_timestamp(actions[-1].get("timeActual"))
        self.last_poll = PollInfo(
            fetch_ms=feed.fetch_ms,
            not_modified=feed.not_modified,
            backfill=backfill,
            feed_age_seconds=None if newest_ts is None else max(0.0, self._clock() - newest_ts),
            new_actions=len(fresh),
            fidelity=self.fidelity,
        )
        return snapshots

    async def _pbp_failed(self, game_id: str, exc: Exception | None = None) -> list[GameSnapshot] | None:
        """The play-by-play request failed. Before tip-off that is normal (the
        CDN has no file yet); during a game, repeated failures mean fall back.

        A refusal (HTTP 401/403) will not clear in a few seconds, so if the
        game is live and the boxscore is readable the fallback engages at once:
        otherwise a host the CDN blocks would get a 503 on a game's first
        connection, because a fresh source starts with no failure count.
        """
        box_ok = await self._refresh_box(game_id, force=True)
        if box_ok and self._status == "scheduled":
            return self._pregame(game_id)
        reason = getattr(exc, "reason", "") or ""
        self._pbp_failures += 1
        logger.warning("Play-by-play fetch failed for %s (%d in a row)", game_id, self._pbp_failures, exc_info=exc)
        refused = reason in ("HTTP 401", "HTTP 403")
        limit = PBP_404_FAILURES_BEFORE_TICKS if reason == "HTTP 404" else PBP_FAILURES_BEFORE_TICKS
        if box_ok and (refused or self._pbp_failures >= limit) and await self._engage_ticks(game_id):
            return await self._tick_poll(game_id)
        return None

    async def _no_actions(self, game_id: str) -> list[GameSnapshot] | None:
        if self._status == "scheduled":
            self._first = False
            return self._pregame(game_id)
        if sum(self._box_scores) > 0 or self._status == "final":
            # The game has scored (or ended) but the feed shows no plays: not usable.
            self._pbp_failures += 1
            logger.warning("%s: play-by-play feed is empty for a game with a score", game_id)
            if self._pbp_failures >= PBP_FAILURES_BEFORE_TICKS and await self._engage_ticks(game_id):
                return await self._tick_poll(game_id)
            return None
        return []  # tip-off not logged yet

    def _pregame(self, game_id: str) -> list[GameSnapshot]:
        if self._pregame_emitted:
            return []
        self._pregame_emitted = True
        return [self._snapshot(game_id, 0, 0.0, 0, 0, "scheduled")]

    def _snapshot(self, game_id, period, clock, home, away, status, **extra) -> GameSnapshot:
        return GameSnapshot(
            game_id=game_id,
            period=period,
            game_clock_seconds_remaining=clock,
            home_team=self._home,
            away_team=self._away,
            home_score=home,
            away_score=away,
            game_status=status,
            exact=True,
            **extra,
        )

    def _snapshots_for(self, game_id: str, fresh: list[dict]) -> list[GameSnapshot]:
        out: list[GameSnapshot] = []
        for action in fresh:
            self._last_action = action["actionNumber"]
            home, away = _score(action.get("scoreHome")), _score(action.get("scoreAway"))
            if home is not None and away is not None:
                self._home_score, self._away_score = home, away
            period = _int(action.get("period"))
            if period <= 0:
                continue
            snapshot = self._snapshot(
                game_id,
                period,
                parse_clock_seconds(action.get("clock")),
                self._home_score,
                self._away_score,
                "live",
                source_ts=parse_timestamp(action.get("timeActual")),
                action_number=action["actionNumber"],
            )
            last = self._last_snapshot
            # Consecutive actions at the same moment (a foul, then a sub...)
            # add nothing for the engine; keep one snapshot per distinct state.
            if last is not None and (last.period, last.game_clock_seconds_remaining, last.home_score, last.away_score) == (
                snapshot.period,
                snapshot.game_clock_seconds_remaining,
                snapshot.home_score,
                snapshot.away_score,
            ):
                continue
            self._last_snapshot = snapshot
            out.append(snapshot)
        return out

    def _apply_final(self, game_id: str, snapshots: list[GameSnapshot]) -> list[GameSnapshot]:
        if self._status != "final" or self._final_emitted or self._last_snapshot is None:
            return snapshots
        behind = (self._home_score, self._away_score) != self._box_scores and not self._game_end_seen
        if behind and self._final_waits < FINAL_WAIT_POLLS:
            self._final_waits += 1  # the boxscore is ahead of the plays; let them catch up
            return snapshots
        self._final_emitted = True
        if snapshots:
            snapshots[-1] = replace(snapshots[-1], game_status="final")
            self._last_snapshot = snapshots[-1]
        else:
            self._last_snapshot = replace(self._last_snapshot, game_status="final", action_number=None, source_ts=None)
            snapshots.append(self._last_snapshot)
        return snapshots

    # -- boxscore-tick fallback ---------------------------------------------

    async def _engage_ticks(self, game_id: str) -> bool:
        if not await self._refresh_box(game_id, force=True):
            return False
        logger.warning("%s: play-by-play unavailable; falling back to boxscore polls (lower fidelity)", game_id)
        self._ticks = True
        self.fidelity = FIDELITY_BOXSCORE_TICKS
        return True

    async def _tick_poll(self, game_id: str) -> list[GameSnapshot] | None:
        try:
            feed = _as_feed(await self._fetch_box(game_id))
            snapshot = snapshot_from_boxscore((feed.data or {})["game"])
        except Exception:
            logger.warning("Boxscore fetch failed for %s", game_id, exc_info=True)
            return None
        self._status = snapshot.game_status
        self.last_poll = PollInfo(feed.fetch_ms, feed.not_modified, False, None, 0, self.fidelity)
        key = lambda s: (s.period, s.game_clock_seconds_remaining, s.home_score, s.away_score, s.game_status)  # noqa: E731
        if self._last_snapshot is not None and key(self._last_snapshot) == key(snapshot):
            return []
        self._last_snapshot = snapshot
        self._changed_at = self._monotonic()
        return [snapshot]

    # -- adaptive cadence ------------------------------------------------------

    def suggest_interval(self, base: float) -> float:
        """Poll at `base` while plays are arriving; ease off when the feed is quiet."""
        if self._status == "scheduled":
            return max(base, settings.live_deep_idle_poll_seconds)
        if self._status == "final":
            return base
        quiet = self._monotonic() - self._changed_at
        if quiet >= VERY_QUIET_SECONDS:
            return max(base, settings.live_deep_idle_poll_seconds)
        if quiet >= QUIET_SECONDS or self._period_ended:
            return max(base, settings.live_idle_poll_seconds)
        return base

"""Replays of recently finished games, built from their play-by-play.

The curated fixtures in data/replay_fixtures were built offline by
backend/historical/build_replay_fixture.py. This does the same thing on
demand for any game that finished in the dashboard's window: one snapshot
per play-by-play action, scores forward-filled, the last one marked final.
The snapshots then go through the same ReplaySource -> poller -> engine path
as a fixture, so the win probability and heat checks are computed exactly
as they would be for a fixture replay (see the README for how replay
cadence differs from live polling).

Only games the server already knows finished (this week's schedule, or
today's scoreboard) can be replayed, so the endpoint can't be used to make
the server fetch arbitrary ids. Built snapshot lists are cached: a finished
game never changes.
"""

from collections import OrderedDict

from app.nba.moments import scoring_events
from app.nba.parse import parse_clock_seconds

MAX_CACHED_GAMES = 64

_cache: "OrderedDict[str, list[dict]]" = OrderedDict()


class NotReplayable(Exception):
    """The game's play-by-play is missing or too short to replay."""


def _score(value) -> int | None:
    text = str(value if value is not None else "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def snapshots_from_playbyplay(pbp: dict, game_id: str, home: str, away: str) -> list[dict]:
    """GameSnapshot dicts, one per action, like the offline fixture builder."""
    if len(scoring_events(pbp)) < 10:
        raise NotReplayable("Not enough play-by-play to replay this game")
    actions = ((pbp or {}).get("game") or {}).get("actions") or []
    snapshots = []
    home_score = away_score = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        try:
            period = int(action.get("period"))
        except (TypeError, ValueError):
            continue
        if period <= 0:
            continue
        new_home, new_away = _score(action.get("scoreHome")), _score(action.get("scoreAway"))
        if new_home is not None and new_away is not None:
            home_score, away_score = new_home, new_away
        snapshot = {
            "game_id": game_id,
            "period": period,
            "game_clock_seconds_remaining": parse_clock_seconds(action.get("clock")),
            "home_team": home,
            "away_team": away,
            "home_score": home_score,
            "away_score": away_score,
            "game_status": "live",
        }
        # Consecutive actions at the same moment (a foul, then a sub...) add
        # nothing for the engine; keep one snapshot per distinct state.
        if snapshots and snapshots[-1] == snapshot:
            continue
        snapshots.append(snapshot)
    snapshots[-1] = {**snapshots[-1], "game_status": "final"}
    return snapshots


async def past_game_snapshots(client, game: dict) -> list[dict]:
    """Cached snapshots for a finished game. `game` needs game_id/home/away."""
    game_id = game["game_id"]
    if game_id in _cache:
        _cache.move_to_end(game_id)
        return _cache[game_id]
    pbp = await client.playbyplay(game_id)
    snapshots = snapshots_from_playbyplay(pbp, game_id, game["home"], game["away"])
    _cache[game_id] = snapshots
    while len(_cache) > MAX_CACHED_GAMES:
        _cache.popitem(last=False)
    return snapshots

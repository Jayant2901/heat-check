"""ESPN as a second data source, translated into NBA.com's CDN shapes.

cdn.nba.com refuses requests from some cloud hosts (it answers HTTP 403 to
Render, for example). ESPN's public site API carries the same information
and generally accepts them. Rather than teach every parser a second
format, this module turns ESPN's JSON into the same dict shapes that
cdn.nba.com returns, so app/nba/parse.py, moments.py, weekly.py and the
live poller work unchanged:

- scoreboard()  <- site.api.espn.com/.../nba/scoreboard?dates=YYYYMMDD
- schedule()    <- the same scoreboard, one request per day of the window
- boxscore(id)  <- site.api.espn.com/.../nba/summary?event=<id>
- playbyplay(id)<- the same summary's `plays`

Game ids are ESPN event ids (e.g. "401705123"), never NBA ids, so
AutoNbaClient (client.py) can tell from an id which source it belongs to.

Every field is read defensively: this mapping was written from ESPN's
documented-by-usage format and could not be checked against a live
response where it was written. /api/nba-check reports whether it works on
a deploy.
"""

import asyncio
import logging
import re
from datetime import timedelta

from app.core.config import settings
from app.nba.client import HttpNbaClient

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
SCOREBOARD_URL = f"{ESPN_BASE}/scoreboard?dates={{day}}"
SUMMARY_URL = f"{ESPN_BASE}/summary?event={{event_id}}"

SCOREBOARD_TTL_SECONDS = 10.0
SCHEDULE_TTL_SECONDS = 30 * 60.0
SUMMARY_TTL_SECONDS = 5.0

# ESPN's abbreviations that differ from NBA.com's tricodes.
TRICODES = {
    "GS": "GSW",
    "NY": "NYK",
    "SA": "SAS",
    "NO": "NOP",
    "UTAH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
    "NJ": "BKN",
    "CHO": "CHA",
}

STATUS_CODES = {"pre": 1, "in": 2, "post": 3}

_CLOCK_TEXT = re.compile(r"^(?:(\d+):)?(\d+(?:\.\d+)?)$")


def tricode(abbreviation) -> str:
    code = str(abbreviation or "").upper()
    return TRICODES.get(code, code)


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def iso_clock(seconds) -> str:
    """Seconds -> NBA.com's 'PT05M12.00S' clock format."""
    try:
        seconds = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    return f"PT{int(seconds // 60):02d}M{seconds % 60:05.2f}S"


def clock_from_text(text) -> float | None:
    """ESPN display clocks: '5:12' -> 312.0, '45.2' (last minute) -> 45.2."""
    match = _CLOCK_TEXT.match(str(text or "").strip())
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    return minutes * 60 + float(match.group(2))


def _status(status: dict) -> tuple[int, int, str, str]:
    """(gameStatus code, period, gameClock, status text) from an ESPN status."""
    status = status or {}
    kind = status.get("type") or {}
    code = STATUS_CODES.get(kind.get("state"), 1)
    period = _int(status.get("period"))
    seconds = status.get("clock")
    if seconds is None:
        seconds = clock_from_text(status.get("displayClock"))
    clock = iso_clock(seconds) if seconds is not None else ""
    text = kind.get("shortDetail") or kind.get("detail") or kind.get("description") or ""
    return code, period, clock, text


def _competition(event: dict) -> dict:
    competitions = (event or {}).get("competitions") or [{}]
    return competitions[0] or {}


def _sides(competition: dict) -> tuple[dict, dict]:
    home = away = {}
    for competitor in competition.get("competitors") or []:
        if competitor.get("homeAway") == "home":
            home = competitor
        elif competitor.get("homeAway") == "away":
            away = competitor
    return home, away


def _record(competitor: dict) -> tuple[int, int]:
    for record in competitor.get("records") or []:
        summary = str(record.get("summary") or "")
        if "-" in summary:
            wins, _, losses = summary.partition("-")
            return _int(wins), _int(losses)
    return 0, 0


def _team(competitor: dict) -> dict:
    team = competitor.get("team") or {}
    wins, losses = _record(competitor)
    return {
        "teamId": team.get("id"),
        "teamTricode": tricode(team.get("abbreviation")),
        "teamCity": team.get("location") or "",
        "teamName": team.get("name") or team.get("shortDisplayName") or "",
        "score": _int(competitor.get("score")),
        "wins": wins,
        "losses": losses,
    }


def _leaders(competitor: dict) -> dict:
    """The points leader, with rebounds/assists only when the same player
    also leads those categories (ESPN lists one leader per category)."""
    by_category = {}
    for category in competitor.get("leaders") or []:
        top = (category.get("leaders") or [None])[0]
        if top:
            by_category[category.get("name")] = top
    points = by_category.get("points")
    if not points:
        return {}
    athlete = points.get("athlete") or {}
    leader = {
        "personId": athlete.get("id"),
        "name": athlete.get("displayName") or athlete.get("shortName") or "",
        "points": _int(points.get("value")),
    }
    for category, key in (("rebounds", "rebounds"), ("assists", "assists")):
        other = by_category.get(category)
        if other and (other.get("athlete") or {}).get("id") == athlete.get("id"):
            leader[key] = _int(other.get("value"))
    return leader


def _utc(value) -> str:
    """ESPN dates look like '2026-11-05T00:30Z'; NBA.com's have seconds."""
    text = str(value or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$", text):
        return text[:-1] + ":00Z"
    return text


def scoreboard_game(event: dict) -> dict:
    competition = _competition(event)
    code, period, clock, text = _status(competition.get("status") or event.get("status"))
    home, away = _sides(competition)
    return {
        "gameId": str(event.get("id") or ""),
        "gameStatus": code,
        "gameStatusText": text,
        "period": period,
        "gameClock": clock,
        "gameTimeUTC": _utc(event.get("date") or competition.get("date")),
        "homeTeam": _team(home),
        "awayTeam": _team(away),
        "gameLeaders": {"homeLeaders": _leaders(home), "awayLeaders": _leaders(away)},
    }


def translate_scoreboard(raw: dict, day_iso: str) -> dict:
    games = [scoreboard_game(e) for e in (raw or {}).get("events") or [] if e]
    return {"scoreboard": {"gameDate": day_iso, "games": [g for g in games if g["gameId"]]}}


def translate_schedule(days: list[tuple[str, dict]]) -> dict:
    game_dates = []
    for day_iso, raw in days:
        games = []
        for game in translate_scoreboard(raw, day_iso)["scoreboard"]["games"]:
            games.append(
                {
                    "gameId": game["gameId"],
                    "gameStatus": game["gameStatus"],
                    "gameDateEst": f"{day_iso}T00:00:00Z",
                    "homeTeam": {"teamTricode": game["homeTeam"]["teamTricode"], "score": game["homeTeam"]["score"]},
                    "awayTeam": {"teamTricode": game["awayTeam"]["teamTricode"], "score": game["awayTeam"]["score"]},
                }
            )
        game_dates.append({"gameDate": day_iso, "games": games})
    return {"leagueSchedule": {"gameDates": game_dates}}


def _made_attempted(text) -> tuple[int, int]:
    made, _, attempted = str(text or "").partition("-")
    return _int(made), _int(attempted)


# Box score stat columns, by ESPN label (falling back to ESPN key names).
_LABEL_ALIASES = {
    "MIN": ("MIN", "minutes"),
    "FG": ("FG", "fieldGoalsMade-fieldGoalsAttempted"),
    "3PT": ("3PT", "threePointFieldGoalsMade-threePointFieldGoalsAttempted"),
    "FT": ("FT", "freeThrowsMade-freeThrowsAttempted"),
    "OREB": ("OREB", "offensiveRebounds"),
    "DREB": ("DREB", "defensiveRebounds"),
    "REB": ("REB", "rebounds"),
    "AST": ("AST", "assists"),
    "STL": ("STL", "steals"),
    "BLK": ("BLK", "blocks"),
    "TO": ("TO", "turnovers"),
    "PF": ("PF", "fouls"),
    "PTS": ("PTS", "points"),
}


def _columns(block: dict) -> dict[str, int]:
    labels = [str(x) for x in block.get("labels") or []]
    keys = [str(x) for x in block.get("keys") or block.get("names") or []]
    columns = {}
    for name, aliases in _LABEL_ALIASES.items():
        for alias in aliases:
            for names in (labels, keys):
                if alias in names:
                    columns[name] = names.index(alias)
                    break
            if name in columns:
                break
    return columns


def _player(entry: dict, columns: dict[str, int]) -> dict:
    athlete = entry.get("athlete") or {}
    stats = entry.get("stats") or []

    def col(name):
        index = columns.get(name)
        return stats[index] if index is not None and index < len(stats) else None

    base = {"personId": athlete.get("id"), "name": athlete.get("displayName") or athlete.get("shortName") or ""}
    if entry.get("didNotPlay") or not stats:
        return {**base, "statistics": {"minutes": ""}}
    fgm, fga = _made_attempted(col("FG"))
    tpm, tpa = _made_attempted(col("3PT"))
    ftm, fta = _made_attempted(col("FT"))
    oreb = _int(col("OREB"))
    reb = _int(col("REB"))
    return {
        **base,
        "statistics": {
            "minutes": iso_clock(_int(col("MIN")) * 60) if _int(col("MIN")) else "",
            "points": _int(col("PTS")),
            "fieldGoalsMade": fgm,
            "fieldGoalsAttempted": fga,
            "threePointersMade": tpm,
            "threePointersAttempted": tpa,
            "freeThrowsMade": ftm,
            "freeThrowsAttempted": fta,
            "reboundsOffensive": oreb,
            "reboundsDefensive": _int(col("DREB"), reb - oreb),
            "reboundsTotal": reb,
            "assists": _int(col("AST")),
            "steals": _int(col("STL")),
            "blocks": _int(col("BLK")),
            "turnovers": _int(col("TO")),
            "foulsPersonal": _int(col("PF")),
        },
    }


def _header_competition(summary: dict) -> dict:
    competitions = ((summary or {}).get("header") or {}).get("competitions") or [{}]
    return competitions[0] or {}


def translate_boxscore(summary: dict, event_id: str) -> dict:
    competition = _header_competition(summary)
    code, period, clock, text = _status(competition.get("status"))
    home, away = _sides(competition)
    home_team, away_team = _team(home), _team(away)

    players_by_team = {}
    for team_block in ((summary or {}).get("boxscore") or {}).get("players") or []:
        team = team_block.get("team") or {}
        players = []
        for block in team_block.get("statistics") or []:
            columns = _columns(block)
            players.extend(_player(entry, columns) for entry in block.get("athletes") or [])
        players_by_team[str(team.get("id"))] = players
        players_by_team[tricode(team.get("abbreviation"))] = players

    def with_players(team: dict) -> dict:
        players = players_by_team.get(str(team.get("teamId"))) or players_by_team.get(team["teamTricode"]) or []
        return {**team, "players": players}

    return {
        "game": {
            "gameId": event_id,
            "gameStatus": code,
            "gameStatusText": text,
            "period": period,
            "gameClock": clock,
            "homeTeam": with_players(home_team),
            "awayTeam": with_players(away_team),
        }
    }


def translate_playbyplay(summary: dict, event_id: str) -> dict:
    competition = _header_competition(summary)
    home, away = _sides(competition)
    codes = {}
    for competitor in (home, away):
        team = competitor.get("team") or {}
        codes[str(team.get("id"))] = tricode(team.get("abbreviation"))

    actions = []
    for play in (summary or {}).get("plays") or []:
        if not isinstance(play, dict):
            continue
        period = _int((play.get("period") or {}).get("number"))
        clock_text = (play.get("clock") or {}).get("displayValue")
        seconds = clock_from_text(clock_text)
        actions.append(
            {
                "period": period,
                "clock": iso_clock(seconds) if seconds is not None else "",
                "teamTricode": codes.get(str((play.get("team") or {}).get("id")), ""),
                "description": play.get("text") or "",
                "scoreHome": "" if play.get("homeScore") is None else str(play.get("homeScore")),
                "scoreAway": "" if play.get("awayScore") is None else str(play.get("awayScore")),
            }
        )
    return {"game": {"gameId": event_id, "actions": actions}}


def is_espn_id(game_id: str) -> bool:
    """NBA.com ids are 10 digits starting "00"; ESPN event ids are not."""
    return not str(game_id).startswith("00")


class EspnNbaClient(HttpNbaClient):
    """Same interface as HttpNbaClient; data from ESPN, in NBA.com shapes."""

    mode = "live"
    source = "espn"

    def __init__(self, today=None, **kwargs) -> None:
        super().__init__(**kwargs)
        if today is None:
            from app.nba.weekly import eastern_today

            today = eastern_today
        self._today = today

    async def scoreboard(self) -> dict:
        day = self._today()
        raw = await self._cached(SCOREBOARD_URL.format(day=f"{day:%Y%m%d}"), SCOREBOARD_TTL_SECONDS)
        return translate_scoreboard(raw, day.isoformat())

    async def schedule(self) -> dict:
        end = self._today()
        days = [end - timedelta(days=n) for n in range(settings.weekly_window_days + 1)]

        async def one(day):
            url = SCOREBOARD_URL.format(day=f"{day:%Y%m%d}")
            # Today's games are still changing; earlier days are settled.
            ttl = SCOREBOARD_TTL_SECONDS if day == end else SCHEDULE_TTL_SECONDS
            return day.isoformat(), await self._cached(url, ttl)

        results = await asyncio.gather(*(one(d) for d in sorted(days)))
        return translate_schedule(list(results))

    async def _summary(self, event_id: str) -> dict:
        # boxscore() and playbyplay() read the same summary, and the weekly
        # build asks for both at once; a short cache makes that one request
        # (and stays fresh enough for live polling every 12 s).
        return await self._cached(SUMMARY_URL.format(event_id=event_id), SUMMARY_TTL_SECONDS)

    async def boxscore(self, game_id: str) -> dict:
        return translate_boxscore(await self._summary(game_id), game_id)

    async def playbyplay(self, game_id: str) -> dict:
        return translate_playbyplay(await self._summary(game_id), game_id)

    async def probe(self) -> list[dict]:
        day = self._today()
        results = []
        board_result, board = await self._probe_one("espn scoreboard", SCOREBOARD_URL.format(day=f"{day:%Y%m%d}"))
        results.append(board_result)
        events = (board or {}).get("events") or []
        if events:
            summary_result, _ = await self._probe_one("espn summary", SUMMARY_URL.format(event_id=events[0].get("id")))
            results.append(summary_result)
        return results

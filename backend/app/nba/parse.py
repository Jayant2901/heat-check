"""Pure parsing helpers for NBA.com's CDN JSON (cdn.nba.com/static/json/...).

Every function here is defensive about missing or malformed fields: the CDN
payloads change shape between pre-game, live and final, and a single odd
game should never take down the scoreboard or the weekly dashboard.

Field names were checked against the sample payloads that ship inside
nba_api's live endpoints (nba_api/live/nba/endpoints/*.py), which mirror the
CDN responses. See the README for what was and wasn't verified against live
responses.
"""

import re
from datetime import date

_CLOCK_RE = re.compile(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$")

STATUS_BY_CODE = {1: "scheduled", 2: "live", 3: "final"}
_STATUS_ORDER = {"live": 0, "scheduled": 1, "final": 2}


def parse_clock_seconds(iso_clock) -> float:
    """'PT05M12.00S' -> 312.0. Also accepts 'PT25M' (no seconds part, as in
    boxscore minutesCalculated). Anything unparseable is 0.0."""
    if not isinstance(iso_clock, str):
        return 0.0
    match = _CLOCK_RE.match(iso_clock.strip())
    if not match:
        return 0.0
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return minutes * 60 + seconds


def period_label(period) -> str:
    """1-4 -> 'Q1'..'Q4', 5 -> 'OT', 6 -> '2OT'. 0 or junk -> ''."""
    try:
        period = int(period)
    except (TypeError, ValueError):
        return ""
    if period <= 0:
        return ""
    if period <= 4:
        return f"Q{period}"
    if period == 5:
        return "OT"
    return f"{period - 4}OT"


def clock_text(period, clock_seconds) -> str:
    """'Q3 5:12'. Empty when the period is unknown."""
    label = period_label(period)
    if not label:
        return ""
    seconds = max(0, int(clock_seconds or 0))
    return f"{label} {seconds // 60}:{seconds % 60:02d}"


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def game_score(stats: dict) -> float:
    """John Hollinger's Game Score from a boxscore `statistics` dict."""
    pts = _int(stats.get("points"))
    fgm = _int(stats.get("fieldGoalsMade"))
    fga = _int(stats.get("fieldGoalsAttempted"))
    ftm = _int(stats.get("freeThrowsMade"))
    fta = _int(stats.get("freeThrowsAttempted"))
    oreb = _int(stats.get("reboundsOffensive"))
    reb = _int(stats.get("reboundsTotal"))
    dreb = _int(stats.get("reboundsDefensive"), reb - oreb)
    stl = _int(stats.get("steals"))
    ast = _int(stats.get("assists"))
    blk = _int(stats.get("blocks"))
    pf = _int(stats.get("foulsPersonal"))
    tov = _int(stats.get("turnovers"))
    score = (
        pts
        + 0.4 * fgm
        - 0.7 * fga
        - 0.4 * (fta - ftm)
        + 0.7 * oreb
        + 0.3 * dreb
        + stl
        + 0.7 * ast
        + 0.7 * blk
        - 0.4 * pf
        - tov
    )
    return round(score, 1)


def _team(raw: dict | None) -> dict:
    raw = raw or {}
    return {
        "tricode": raw.get("teamTricode") or "",
        "city": raw.get("teamCity") or "",
        "name": raw.get("teamName") or "",
        "score": _int(raw.get("score")),
        "wins": _int(raw.get("wins")),
        "losses": _int(raw.get("losses")),
    }


def _leader(raw: dict | None) -> dict | None:
    if not raw or not raw.get("name"):
        return None
    return {
        "person_id": raw.get("personId"),
        "name": raw.get("name"),
        "points": _int(raw.get("points")),
        # None when the source doesn't say (ESPN lists one leader per stat).
        "rebounds": _int(raw["rebounds"]) if raw.get("rebounds") is not None else None,
        "assists": _int(raw["assists"]) if raw.get("assists") is not None else None,
    }


def parse_scoreboard(raw: dict) -> dict:
    """todaysScoreboard_00.json -> {"game_date", "games": [...]}, with games
    ordered live, then scheduled, then final (by tip-off time in a group)."""
    board = (raw or {}).get("scoreboard") or {}
    games = []
    for game in board.get("games") or []:
        if not isinstance(game, dict) or not game.get("gameId"):
            continue
        status = STATUS_BY_CODE.get(_int(game.get("gameStatus"), 1), "scheduled")
        period = _int(game.get("period"))
        clock_seconds = parse_clock_seconds(game.get("gameClock"))
        leaders = game.get("gameLeaders") or {}
        games.append(
            {
                "game_id": game["gameId"],
                "status": status,
                "status_text": (game.get("gameStatusText") or "").strip(),
                "period": period,
                "clock_seconds": clock_seconds,
                "clock_text": clock_text(period, clock_seconds) if status == "live" else "",
                "start_time_utc": game.get("gameTimeUTC") or "",
                "home": _team(game.get("homeTeam")),
                "away": _team(game.get("awayTeam")),
                "home_leader": _leader(leaders.get("homeLeaders")),
                "away_leader": _leader(leaders.get("awayLeaders")),
            }
        )
    games.sort(key=lambda g: (_STATUS_ORDER[g["status"]], g["start_time_utc"]))
    return {"game_date": board.get("gameDate") or "", "games": games}


def parse_final_games(raw: dict, start: date, end: date) -> list[dict]:
    """scheduleLeagueV2.json -> finished games whose Eastern date falls in
    [start, end] inclusive, oldest first."""
    schedule = (raw or {}).get("leagueSchedule") or {}
    out = []
    for game_date in schedule.get("gameDates") or []:
        for game in (game_date or {}).get("games") or []:
            if not isinstance(game, dict) or _int(game.get("gameStatus")) != 3:
                continue
            day_text = (game.get("gameDateEst") or "")[:10]
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            home = game.get("homeTeam") or {}
            away = game.get("awayTeam") or {}
            out.append(
                {
                    "game_id": game.get("gameId"),
                    "date": day.isoformat(),
                    "home": home.get("teamTricode") or "",
                    "away": away.get("teamTricode") or "",
                    "home_score": _int(home.get("score")),
                    "away_score": _int(away.get("score")),
                }
            )
    out = [g for g in out if g["game_id"]]
    out.sort(key=lambda g: (g["date"], g["game_id"]))
    return out


def _minutes(stats: dict) -> float:
    for key in ("minutes", "minutesCalculated"):
        value = parse_clock_seconds(stats.get(key))
        if value > 0:
            return round(value / 60, 1)
    return 0.0


def parse_boxscore_players(raw: dict) -> list[dict]:
    """boxscore_{id}.json -> one row per player who actually played."""
    game = (raw or {}).get("game") or {}
    home = game.get("homeTeam") or {}
    away = game.get("awayTeam") or {}
    rows = []
    for side, team, opponent in (("home", home, away), ("away", away, home)):
        for player in team.get("players") or []:
            stats = (player or {}).get("statistics") or {}
            minutes = _minutes(stats)
            if minutes <= 0:
                continue
            rows.append(
                {
                    "person_id": player.get("personId"),
                    "name": player.get("name") or "",
                    "team": team.get("teamTricode") or "",
                    "opponent": opponent.get("teamTricode") or "",
                    "side": side,
                    "minutes": minutes,
                    "pts": _int(stats.get("points")),
                    "reb": _int(stats.get("reboundsTotal")),
                    "ast": _int(stats.get("assists")),
                    "stl": _int(stats.get("steals")),
                    "blk": _int(stats.get("blocks")),
                    "tov": _int(stats.get("turnovers")),
                    "fgm": _int(stats.get("fieldGoalsMade")),
                    "fga": _int(stats.get("fieldGoalsAttempted")),
                    "fg3m": _int(stats.get("threePointersMade")),
                    "fg3a": _int(stats.get("threePointersAttempted")),
                    "ftm": _int(stats.get("freeThrowsMade")),
                    "fta": _int(stats.get("freeThrowsAttempted")),
                    "game_score": game_score(stats),
                }
            )
    return rows

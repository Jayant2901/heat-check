"""Offline sample data in NBA.com's CDN shapes (HEAT_CHECK_DATA=demo).

Everything here is generated: fictional players on real team codes, random
final scores, and a synthetic play-by-play with one deliberate "hot stretch"
per game so the dashboard has something to find. It exists so the UI can be
built and shown without network access. Every API response in demo mode
carries mode: "demo", and the UI shows a "Sample data" banner.

The two "live" games stream archived replay fixtures at 10x speed; the
scheduled games stream a single pre-game 0-0 snapshot.
"""

import json
import random
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache

from app.core.config import REPLAY_FIXTURES_DIR, settings
from app.core.interfaces import DataSource, GameSnapshot
from app.datasources.replay_source import ReplaySource
from app.nba.weekly import eastern_today

TEAMS = {
    "ATL": ("Atlanta", "Hawks"), "BOS": ("Boston", "Celtics"), "BKN": ("Brooklyn", "Nets"),
    "CHA": ("Charlotte", "Hornets"), "CHI": ("Chicago", "Bulls"), "CLE": ("Cleveland", "Cavaliers"),
    "DAL": ("Dallas", "Mavericks"), "DEN": ("Denver", "Nuggets"), "DET": ("Detroit", "Pistons"),
    "GSW": ("Golden State", "Warriors"), "HOU": ("Houston", "Rockets"), "IND": ("Indiana", "Pacers"),
    "LAC": ("LA", "Clippers"), "LAL": ("Los Angeles", "Lakers"), "MEM": ("Memphis", "Grizzlies"),
    "MIA": ("Miami", "Heat"), "MIL": ("Milwaukee", "Bucks"), "MIN": ("Minnesota", "Timberwolves"),
    "NOP": ("New Orleans", "Pelicans"), "NYK": ("New York", "Knicks"), "OKC": ("Oklahoma City", "Thunder"),
    "ORL": ("Orlando", "Magic"), "PHI": ("Philadelphia", "76ers"), "PHX": ("Phoenix", "Suns"),
    "POR": ("Portland", "Trail Blazers"), "SAC": ("Sacramento", "Kings"), "SAS": ("San Antonio", "Spurs"),
    "TOR": ("Toronto", "Raptors"), "UTA": ("Utah", "Jazz"), "WAS": ("Washington", "Wizards"),
}

FIRST_NAMES = (
    "Avery", "Brooks", "Cal", "Dante", "Eli", "Felix", "Gus", "Hollis", "Isaiah", "Jalen", "Kai",
    "Lonnie", "Marcus", "Nico", "Otis", "Percy", "Quinn", "Reggie", "Sol", "Theo", "Uriah", "Vance",
    "Wes", "Xavier", "Yusuf", "Zeke",
)
LAST_NAMES = (
    "Abernathy", "Banks", "Calloway", "Dorsey", "Ellison", "Fairbanks", "Grimes", "Holloway", "Ingram",
    "Jarrett", "Keller", "Lockhart", "Mercer", "Norwood", "Oakley", "Pruitt", "Quarles", "Ramsey",
    "Stroud", "Tillman", "Underwood", "Vickers", "Whitfield", "Yancey",
)

# (demo id, replay fixture id) -- the "live" games play archived fixtures.
LIVE_GAMES = {"demo-live-1": "0022200001", "demo-live-2": "0022301034"}
SCHEDULED_GAMES = {"demo-sched-1": ("NYK", "MIA", 0), "demo-sched-2": ("LAL", "DEN", 90)}  # minutes after 7pm ET
FINAL_TODAY = {"demo-final-1": ("CLE", "MIL")}
REPLAY_SPEED = 10.0


def _clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"PT{int(seconds // 60):02d}M{seconds % 60:05.2f}S"


@lru_cache(maxsize=None)
def _roster(team: str) -> tuple[tuple[int, str], ...]:
    rng = random.Random(f"roster-{team}")
    names = set()
    while len(names) < 10:
        names.add(f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}")
    base = 1_000_000 + list(TEAMS).index(team) * 100
    return tuple((base + i, name) for i, name in enumerate(sorted(names)))


@lru_cache(maxsize=256)
def _simulate(game_id: str, home: str, away: str) -> tuple[int, int, tuple[dict, ...]]:
    """A full synthetic play-by-play: (home_score, away_score, actions)."""
    rng = random.Random(game_id)
    hot_side = rng.choice(("home", "away"))
    hot_period = rng.randint(1, 4)
    hot_start = rng.uniform(200, 600)  # clock seconds remaining when it starts
    actions = []
    home_score = away_score = 0
    names = {"home": _roster(home), "away": _roster(away)}
    codes = {"home": home, "away": away}
    period = 1
    while True:
        length = 720 if period <= 4 else 300
        clock = float(length)
        side = rng.choice(("home", "away"))
        while clock > 0:
            hot = period == hot_period and hot_start - 75 < clock < hot_start
            # The hot stretch is quicker: stops, quick shots, fast breaks.
            clock -= rng.uniform(5, 10) if hot else rng.uniform(10, 20)
            if clock <= 0:
                break
            if hot:
                make_chance = 0.97 if side == hot_side else 0.0
            else:
                # A gentle pull back toward a close game keeps the margins
                # in a realistic range instead of a pure random walk.
                lead = (home_score - away_score) * (1 if side == "home" else -1)
                make_chance = min(0.7, max(0.3, 0.5 - 0.012 * lead))
            shooter = rng.choice(names[side][:8])[1]
            if rng.random() < make_chance:
                roll = rng.random()
                points = 3 if roll < 0.33 else (1 if roll < 0.43 else 2)
                if side == "home":
                    home_score += points
                else:
                    away_score += points
                kind = {3: "3PT Jump Shot", 2: "Driving Layup", 1: "Free Throw 1 of 1"}[points]
                description = f"{shooter} {kind} ({points} PTS)"
            else:
                description = f"MISS {shooter} Jump Shot"
            actions.append(
                {
                    "period": period,
                    "clock": _clock(clock),
                    "teamTricode": codes[side],
                    "description": description,
                    # Like the real feed, the score is only filled in on some
                    # non-scoring rows -- exercise both cases.
                    "scoreHome": str(home_score) if not description.startswith("MISS") or rng.random() < 0.5 else "",
                    "scoreAway": str(away_score) if not description.startswith("MISS") or rng.random() < 0.5 else "",
                }
            )
            side = "away" if side == "home" else "home"
        if period >= 4 and home_score != away_score:
            break
        period += 1
    return home_score, away_score, tuple(actions)


def _box_players(team: str, total_points: int, rng: random.Random) -> list[dict]:
    roster = _roster(team)
    weights = [rng.uniform(0.4, 1.0) * (1.8 if i < 3 else 1.0) for i in range(8)]
    shares = [w / sum(weights) for w in weights]
    points = [int(total_points * s) for s in shares]
    points[0] += total_points - sum(points)
    players = []
    for i, (person_id, name) in enumerate(roster):
        if i >= 8:  # last two on the bench: did not play
            players.append({"personId": person_id, "name": name, "statistics": {"minutes": "", "points": 0}})
            continue
        pts = points[i]
        fg3m = rng.randint(0, max(0, pts // 6))
        ftm = rng.randint(0, min(8, max(0, pts - 3 * fg3m)))
        rest = pts - 3 * fg3m - ftm
        if rest % 2:
            ftm += 1
            rest -= 1
        fgm = rest // 2 + fg3m
        oreb = rng.randint(0, 3)
        minutes = rng.uniform(16, 38) if i < 5 else rng.uniform(8, 24)
        players.append(
            {
                "personId": person_id,
                "name": name,
                "statistics": {
                    "minutes": _clock(minutes * 60),
                    "points": pts,
                    "fieldGoalsMade": fgm,
                    "fieldGoalsAttempted": fgm + rng.randint(fgm // 2, fgm + 3),
                    "threePointersMade": fg3m,
                    "threePointersAttempted": fg3m + rng.randint(0, 4),
                    "freeThrowsMade": ftm,
                    "freeThrowsAttempted": ftm + rng.randint(0, 2),
                    "reboundsOffensive": oreb,
                    "reboundsDefensive": rng.randint(1, 9),
                    "reboundsTotal": 0,  # filled below
                    "assists": rng.randint(0, 9 if i < 3 else 4),
                    "steals": rng.randint(0, 3),
                    "blocks": rng.randint(0, 3),
                    "turnovers": rng.randint(0, 4),
                    "foulsPersonal": rng.randint(0, 5),
                },
            }
        )
        stats = players[-1]["statistics"]
        stats["reboundsTotal"] = stats["reboundsOffensive"] + stats["reboundsDefensive"]
    return players


@lru_cache(maxsize=None)
def _fixture(fixture_id: str) -> dict:
    return json.loads((REPLAY_FIXTURES_DIR / f"{fixture_id}.json").read_text())


def _live_start_index(game_id: str) -> int:
    """Where a demo "live" game is when you tune in: its scoreboard tile and
    its stream both start from this snapshot, so they agree."""
    n = list(LIVE_GAMES).index(game_id)
    snapshots = _fixture(LIVE_GAMES[game_id])["snapshots"]
    return int(len(snapshots) * (0.45 + 0.25 * n))


class _PregameSource:
    """Always reports the same scheduled 0-0 snapshot."""

    def __init__(self, game_id: str, home: str, away: str) -> None:
        self._snapshot = GameSnapshot(
            game_id=game_id, period=0, game_clock_seconds_remaining=0.0, home_team=home,
            away_team=away, home_score=0, away_score=0, game_status="scheduled",
        )

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        return self._snapshot

    async def next_snapshots(self, game_id: str) -> list[GameSnapshot] | None:
        return [self._snapshot]


class DemoNbaClient:
    mode = "demo"

    def __init__(self, today=eastern_today) -> None:
        self._today = today

    # -- schedule ---------------------------------------------------------

    def _past_games(self) -> list[dict]:
        today = self._today()
        games = []
        for days_ago in range(1, 9):
            day = today - timedelta(days=days_ago)
            rng = random.Random(f"day-{day.isoformat()}")
            teams = rng.sample(list(TEAMS), 2 * rng.randint(3, 5))
            for n in range(len(teams) // 2):
                games.append((f"demo-{day:%Y%m%d}-{n}", day, teams[2 * n], teams[2 * n + 1]))
        for game_id, (home, away) in FINAL_TODAY.items():
            games.append((game_id, today, home, away))
        return games

    async def schedule(self) -> dict:
        dates: dict[str, list] = {}
        for game_id, day, home, away in self._past_games():
            home_score, away_score, _ = _simulate(game_id, home, away)
            dates.setdefault(day.isoformat(), []).append(
                {
                    "gameId": game_id,
                    "gameStatus": 3,
                    "gameDateEst": f"{day.isoformat()}T00:00:00Z",
                    "homeTeam": {"teamTricode": home, "score": home_score},
                    "awayTeam": {"teamTricode": away, "score": away_score},
                }
            )
        return {"leagueSchedule": {"gameDates": [{"games": g} for g in dates.values()]}}

    def _find(self, game_id: str) -> tuple[str, str]:
        for gid, _day, home, away in self._past_games():
            if gid == game_id:
                return home, away
        raise KeyError(f"Unknown demo game {game_id}")

    async def boxscore(self, game_id: str) -> dict:
        home, away = self._find(game_id)
        home_score, away_score, _ = _simulate(game_id, home, away)
        rng = random.Random(f"box-{game_id}")
        return {
            "game": {
                "gameId": game_id,
                "gameStatus": 3,
                "homeTeam": {"teamTricode": home, "score": home_score, "players": _box_players(home, home_score, rng)},
                "awayTeam": {"teamTricode": away, "score": away_score, "players": _box_players(away, away_score, rng)},
            }
        }

    async def playbyplay(self, game_id: str) -> dict:
        home, away = self._find(game_id)
        _, _, actions = _simulate(game_id, home, away)
        return {"game": {"gameId": game_id, "actions": [dict(a) for a in actions]}}

    # -- today --------------------------------------------------------------

    async def scoreboard(self) -> dict:
        today = self._today()
        # 00:00 UTC is 7pm Eastern (EST) on `today`.
        tip = datetime.combine(today + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)
        games = []
        for n, (game_id, fixture_id) in enumerate(LIVE_GAMES.items()):
            fixture = _fixture(fixture_id)
            snap = fixture["snapshots"][_live_start_index(game_id)]
            games.append(
                self._board_game(
                    game_id, 2, snap["period"], _clock(snap["game_clock_seconds_remaining"]),
                    tip - timedelta(hours=1), snap["home_team"], snap["away_team"],
                    snap["home_score"], snap["away_score"],
                )
            )
        for game_id, (home, away, offset) in SCHEDULED_GAMES.items():
            games.append(self._board_game(game_id, 1, 0, "", tip + timedelta(minutes=offset), home, away, 0, 0))
        for game_id, (home, away) in FINAL_TODAY.items():
            home_score, away_score, _ = _simulate(game_id, home, away)
            games.append(
                self._board_game(game_id, 3, 4, "PT00M00.00S", tip - timedelta(hours=4), home, away, home_score, away_score)
            )
        return {"scoreboard": {"gameDate": today.isoformat(), "games": games}}

    def _board_game(self, game_id, status, period, clock, start, home, away, home_score, away_score) -> dict:
        rng = random.Random(f"board-{game_id}")
        eastern = start - timedelta(hours=5)
        hour = eastern.hour % 12 or 12
        status_text = {1: f"{hour}:{eastern.minute:02d} pm ET", 2: f"Q{period}", 3: "Final"}[status]

        def team(code, score):
            city, name = TEAMS.get(code, (code, code))
            return {
                "teamTricode": code, "teamCity": city, "teamName": name, "score": score,
                "wins": rng.randint(0, 6), "losses": rng.randint(0, 6),
            }

        def leader(code, score):
            if status == 1:
                return {"personId": 0, "name": "", "points": 0, "rebounds": 0, "assists": 0}
            person_id, name = _roster(code)[rng.randint(0, 2)]
            return {
                "personId": person_id, "name": name, "points": max(2, int(score * rng.uniform(0.18, 0.3))),
                "rebounds": rng.randint(2, 11), "assists": rng.randint(1, 9),
            }

        return {
            "gameId": game_id,
            "gameStatus": status,
            "gameStatusText": status_text,
            "period": period,
            "gameClock": clock,
            "gameTimeUTC": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "homeTeam": team(home, home_score),
            "awayTeam": team(away, away_score),
            "gameLeaders": {"homeLeaders": leader(home, home_score), "awayLeaders": leader(away, away_score)},
        }

    def live_source(self, game_id: str, teams=None) -> tuple[DataSource, float]:
        if game_id in LIVE_GAMES:
            path = REPLAY_FIXTURES_DIR / f"{LIVE_GAMES[game_id]}.json"
            return ReplaySource(path, start_index=_live_start_index(game_id)), settings.poll_interval_seconds / REPLAY_SPEED
        if game_id in SCHEDULED_GAMES:
            home, away, _ = SCHEDULED_GAMES[game_id]
            return _PregameSource(game_id, home, away), settings.poll_interval_seconds
        if game_id in FINAL_TODAY:
            home, away = FINAL_TODAY[game_id]
            home_score, away_score, _ = _simulate(game_id, home, away)
            snapshot = GameSnapshot(game_id, 4, 0.0, home, away, home_score, away_score, "final")
            return _OneShot(snapshot), settings.poll_interval_seconds
        raise KeyError(f"Unknown demo game {game_id}")


class _OneShot:
    def __init__(self, snapshot: GameSnapshot) -> None:
        self._snapshot = snapshot

    async def next_snapshot(self, game_id: str) -> GameSnapshot | None:
        return self._snapshot

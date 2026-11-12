"""Download and cache finished NBA games from ESPN's public site API.

Shared by backtest_heat_checks.py and calibration_check.py. Each finished
game's summary is trimmed to what the engine needs (the two teams, the final
score and every play) and cached as data/backtest_cache/<event id>.json, so a
later run does not fetch it again. The cache is git-ignored.

Requests are sequential with a short pause: this is someone else's public API.
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

CACHE_DIR = ROOT / "data" / "backtest_cache"
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
PAUSE_SECONDS = 0.4
PLAY_KEYS = ("sequenceNumber", "type", "text", "awayScore", "homeScore", "period", "clock", "scoringPlay", "team", "wallclock")


def _get(client: httpx.Client, url: str) -> dict | None:
    for attempt in range(3):
        try:
            response = client.get(url, timeout=30)
            if response.status_code == 200:
                return response.json()
            print(f"  HTTP {response.status_code} for {url}", file=sys.stderr)
        except httpx.HTTPError as exc:
            print(f"  {type(exc).__name__} for {url}", file=sys.stderr)
        time.sleep(1.5 * (attempt + 1))
    return None


def trim(summary: dict) -> dict:
    competitions = ((summary.get("header") or {}).get("competitions")) or [{}]
    return {
        "header": {"competitions": competitions},
        "plays": [{k: p[k] for k in PLAY_KEYS if k in p} for p in summary.get("plays") or []],
    }


def finished_event_ids(client: httpx.Client, days: int, today: date) -> list[tuple[str, str]]:
    """(event id, day) for every finished regular-season game in the last `days` days."""
    found = []
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        board = _get(client, f"{BASE}/scoreboard?dates={day:%Y%m%d}")
        time.sleep(PAUSE_SECONDS)
        for event in (board or {}).get("events") or []:
            status = ((event.get("status") or {}).get("type") or {}).get("state")
            # season type 2 = regular season (1 = preseason, 3 = playoffs, ...):
            # preseason games are not played like real ones.
            if status == "post" and (event.get("season") or {}).get("type") == 2:
                found.append((str(event["id"]), day.isoformat()))
    return found


def fetch_games(days: int, today: date | None = None) -> list[dict]:
    """Cached trimmed summaries for finished games in the last `days` days,
    downloading any that are missing. Each has an added "event_id" and "day"."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    games = []
    with httpx.Client(headers={"User-Agent": "heat-check-backtest"}, follow_redirects=True) as client:
        events = finished_event_ids(client, days, today)
        print(f"{len(events)} finished regular-season games on the scoreboards of the last {days} days")
        for n, (event_id, day) in enumerate(events, 1):
            path = CACHE_DIR / f"{event_id}.json"
            if not path.exists():
                summary = _get(client, f"{BASE}/summary?event={event_id}")
                time.sleep(PAUSE_SECONDS)
                if summary is None:
                    continue
                path.write_text(json.dumps({**trim(summary), "event_id": event_id, "day": day}, separators=(",", ":")))
                print(f"  [{n}/{len(events)}] fetched {event_id}")
            games.append(json.loads(path.read_text()))
    return games


def load_cached() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(CACHE_DIR.glob("*.json"))]


def to_pbp(game: dict) -> tuple[dict, str, str]:
    """(NBA-shaped play-by-play, home tricode, away tricode) for a cached game."""
    from app.nba import espn

    summary = {"header": game["header"], "plays": game["plays"]}
    competition = game["header"]["competitions"][0]
    home, away = espn._sides(competition)
    home_code = espn.tricode((home.get("team") or {}).get("abbreviation"))
    away_code = espn.tricode((away.get("team") or {}).get("abbreviation"))
    return espn.translate_playbyplay(summary, game["event_id"]), home_code, away_code

"""Builds the dashboard's "this week" data from finished games.

Window: the last WEEKLY_WINDOW_DAYS days in US/Eastern (the NBA's schedule
date), plus any of today's games that have already finished.

The build runs in the background: snapshot() never blocks a request. Each
finished game's analysis is cached for the life of the process (a final
never changes), so after the first build a refresh only fetches games that
finished since. The aggregate is rebuilt every WEEKLY_REFRESH_SECONDS; while
that happens the previous payload keeps being served.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.core.config import settings
from app.nba.moments import analyze_game
from app.nba.parse import parse_boxscore_players, parse_final_games

logger = logging.getLogger(__name__)

CONCURRENCY = 6
ERROR_RETRY_SECONDS = 30.0
TOP_PERFORMANCES = 10
LEADERS = 3
WEEK_AVERAGES = 5
TOP_SWINGS = 8
TOP_RUNS = 6


def eastern_today() -> date:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # no tz database installed: EST is close enough for a date
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def _stat_line(p: dict) -> str:
    parts = [f"{p['pts']} pts", f"{p['reb']} reb", f"{p['ast']} ast"]
    if p["stl"] >= 3:
        parts.append(f"{p['stl']} stl")
    if p["blk"] >= 3:
        parts.append(f"{p['blk']} blk")
    return ", ".join(parts)


def _result(team_score: int, opp_score: int) -> str:
    letter = "W" if team_score > opp_score else "L"
    return f"{letter} {team_score}-{opp_score}"


class WeeklyService:
    def __init__(
        self,
        client,
        model,
        baseline,
        window_days: int | None = None,
        refresh_seconds: float | None = None,
        run_min_z: float | None = None,
        concurrency: int = CONCURRENCY,
        today=eastern_today,
    ) -> None:
        self.client = client
        self.model = model
        self.baseline = baseline
        self.window_days = window_days if window_days is not None else settings.weekly_window_days
        self.refresh_seconds = refresh_seconds if refresh_seconds is not None else settings.weekly_refresh_seconds
        self.run_min_z = run_min_z if run_min_z is not None else settings.weekly_run_min_z
        self.concurrency = concurrency
        self._today = today

        self._analyses: dict[str, dict] = {}  # game_id -> analysis, kept forever
        self._payload: dict | None = None
        self._built_at = 0.0
        self._task: asyncio.Task | None = None
        self._progress = {"done": 0, "total": 0}
        self._error: str | None = None
        self._error_at = 0.0

    # -- public ---------------------------------------------------------

    def snapshot(self) -> dict:
        """Current state; starts a (re)build in the background if needed.
        Must be called from inside a running event loop."""
        now = time.monotonic()
        building = self._task is not None and not self._task.done()
        stale = self._payload is None or now - self._built_at >= self.refresh_seconds
        recently_failed = self._error is not None and now - self._error_at < ERROR_RETRY_SECONDS
        if stale and not building and not recently_failed:
            self._task = asyncio.create_task(self._build_safely())
            building = True

        if self._payload is not None:
            return self._payload
        if self._error is not None and not building:
            return {"status": "error", "message": self._error}
        return {"status": "building", "progress": dict(self._progress)}

    async def build(self) -> dict:
        """Run one full build now and return the ready payload."""
        end = self._today()
        start = end - timedelta(days=self.window_days)
        games = parse_final_games(await self.client.schedule(), start, end)
        self._progress = {"done": 0, "total": len(games)}

        semaphore = asyncio.Semaphore(self.concurrency)
        skipped = 0

        async def one(game: dict) -> None:
            nonlocal skipped
            game_id = game["game_id"]
            if game_id not in self._analyses:
                try:
                    async with semaphore:
                        self._analyses[game_id] = await self._analyze(game)
                except Exception:
                    logger.warning("Skipping %s in the weekly build", game_id, exc_info=True)
                    skipped += 1
            self._progress["done"] += 1

        await asyncio.gather(*(one(g) for g in games))
        analyzed = [self._analyses[g["game_id"]] for g in games if g["game_id"] in self._analyses]
        payload = self._aggregate(analyzed, start, end, skipped)
        self._payload = payload
        self._built_at = time.monotonic()
        self._error = None
        return payload

    # -- internals ------------------------------------------------------

    async def _build_safely(self) -> None:
        try:
            await self.build()
        except Exception as exc:
            logger.warning("Weekly build failed", exc_info=True)
            from app.nba.client import describe_error

            self._error = f"Couldn't load this week's games from NBA.com ({describe_error(exc)})."
            self._error_at = time.monotonic()

    async def _analyze(self, game: dict) -> dict:
        box, pbp = await asyncio.gather(
            self.client.boxscore(game["game_id"]),
            self.client.playbyplay(game["game_id"]),
        )
        # Parsing and the model call are CPU-bound; keep them off the loop.
        return await asyncio.to_thread(self._analyze_sync, game, box, pbp)

    def _analyze_sync(self, game: dict, box: dict, pbp: dict) -> dict:
        players = parse_boxscore_players(box)
        moments = analyze_game(pbp, self.model, self.baseline, game["home"], game["away"], self.run_min_z)
        scores = {game["home"]: game["home_score"], game["away"]: game["away_score"]}
        for p in players:
            p["date"] = game["date"]
            p["game_id"] = game["game_id"]
            p["fg"] = f"{p['fgm']}-{p['fga']}"
            p["line"] = _stat_line(p)
            p["result"] = _result(scores.get(p["team"], 0), scores.get(p["opponent"], 0))
        return {"game": game, "players": players, "moments": moments}

    def _aggregate(self, analyzed: list[dict], start: date, end: date, skipped: int) -> dict:
        players = [p for a in analyzed for p in a["players"]]

        def perf(p: dict) -> dict:
            keys = (
                "name", "team", "opponent", "date", "game_id", "minutes", "pts", "reb",
                "ast", "stl", "blk", "fg", "game_score", "result", "line",
            )
            return {k: p[k] for k in keys}

        top = sorted(players, key=lambda p: p["game_score"], reverse=True)[:TOP_PERFORMANCES]

        def leaders(stat: str) -> list[dict]:
            best = sorted(players, key=lambda p: (p[stat], p["game_score"]), reverse=True)[:LEADERS]
            return [
                {
                    "name": p["name"], "team": p["team"], "value": p[stat], "opponent": p["opponent"],
                    "date": p["date"], "game_id": p["game_id"],
                }
                for p in best
            ]

        by_player: dict = defaultdict(list)
        for p in players:
            by_player[p["person_id"] or (p["name"], p["team"])].append(p)
        averages = []
        for rows in by_player.values():
            if len(rows) < 2:
                continue
            n = len(rows)
            averages.append(
                {
                    "name": rows[-1]["name"],
                    "team": rows[-1]["team"],
                    "games": n,
                    "avg_game_score": round(sum(r["game_score"] for r in rows) / n, 1),
                    "avg_pts": round(sum(r["pts"] for r in rows) / n, 1),
                    "avg_reb": round(sum(r["reb"] for r in rows) / n, 1),
                    "avg_ast": round(sum(r["ast"] for r in rows) / n, 1),
                }
            )
        averages.sort(key=lambda a: a["avg_game_score"], reverse=True)

        swings, runs, cards = [], [], []
        for a in analyzed:
            g = a["game"]
            extra = {"game_id": g["game_id"], "date": g["date"], "matchup": f"{g['away']} at {g['home']}"}
            swings.extend({**s, **extra} for s in a["moments"]["swings"])
            runs.extend({**r, **extra} for r in a["moments"]["runs"])
            best = max(a["players"], key=lambda p: p["game_score"], default=None)
            cards.append(
                {
                    "game_id": g["game_id"],
                    "date": g["date"],
                    "home": g["home"],
                    "away": g["away"],
                    "home_score": g["home_score"],
                    "away_score": g["away_score"],
                    "winner": g["home"] if g["home_score"] > g["away_score"] else g["away"],
                    "lead_changes": a["moments"]["lead_changes"],
                    "largest_lead": a["moments"]["largest_lead"],
                    "largest_lead_team": a["moments"]["largest_lead_team"],
                    "top_performer": (
                        {"name": best["name"], "team": best["team"], "line": best["line"], "game_score": best["game_score"]}
                        if best
                        else None
                    ),
                }
            )
        swings.sort(key=lambda s: s["swing"], reverse=True)
        runs.sort(key=lambda r: r["z_score"], reverse=True)
        cards.sort(key=lambda c: (c["date"], c["game_id"]), reverse=True)

        return {
            "status": "ready",
            "mode": getattr(self.client, "mode", "live"),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "games_analyzed": len(analyzed),
            "games_skipped": skipped,
            "top_performances": [perf(p) for p in top],
            "leaders": {
                "points": leaders("pts"),
                "rebounds": leaders("reb"),
                "assists": leaders("ast"),
            },
            "week_averages": averages[:WEEK_AVERAGES],
            "moments": {"swings": swings[:TOP_SWINGS], "runs": runs[:TOP_RUNS]},
            "games": cards,
        }


_service: WeeklyService | None = None


def get_weekly_service() -> WeeklyService:
    global _service
    if _service is None:
        from app.core.artifacts import get_baseline, get_model
        from app.nba.client import get_nba_client

        _service = WeeklyService(get_nba_client(), get_model(), get_baseline())
    return _service


def reset_weekly_service() -> None:
    global _service
    _service = None

"""Pull per-game play-by-play (score-margin-over-time) for a fetched season.

Requires fetch_games.py to have already run for the season (reads its
games.parquet for the game ID list). Uses PlayByPlayV3 (V2 is confirmed dead
by nba_api itself -- it now returns empty JSON, so there's no real fallback
to fall back to). Resumable per game_id.

Fetches with a small thread pool -- --workers controls how many requests run
concurrently, --sleep is the pause each *individual* thread takes between its
own requests. Observed directly while building this: nba.com's stats
endpoints have no documented rate limit, but a sustained run of concurrent
requests can trigger a temporary soft-block where every request times out,
seemingly independent of how gently --sleep paces each thread -- it looks
tied to concurrent connection count more than raw request rate. Retrying
blindly during a block just burns through the game list failing every
request, so a CircuitBreaker below pauses ALL workers for a cooldown once
failures cluster, rather than hammering through them.
"""

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import playbyplayv3
from tenacity import retry, stop_after_attempt, wait_exponential

from historical.fetch_games import RAW_DIR, unique_game_ids

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


class CircuitBreaker:
    """Pauses every worker for a cooldown after a run of consecutive failures.

    See module docstring -- this is a direct response to an empirically
    observed soft-block from nba.com's stats endpoints, not a hypothetical.
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 90.0):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._blocked_until = 0.0

    def wait_if_tripped(self) -> None:
        with self._lock:
            blocked_until = self._blocked_until
        remaining = blocked_until - time.monotonic()
        if remaining > 0:
            _log(f"[circuit-breaker] cooling down {remaining:.0f}s before next request...")
            time.sleep(remaining)

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        tripped = False
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._blocked_until = time.monotonic() + self._cooldown_seconds
                self._consecutive_failures = 0
                tripped = True
        if tripped:
            _log(f"[circuit-breaker] TRIPPED -- pausing all workers for {self._cooldown_seconds:.0f}s")


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=8))
def fetch_game_pbp(game_id: str, timeout: float = 10.0) -> pd.DataFrame:
    df = playbyplayv3.PlayByPlayV3(game_id=game_id, timeout=timeout).get_data_frames()[0]
    df["pbpSource"] = "v3"
    return df


def _fetch_one(
    game_id: str, out_dir: Path, sleep_seconds: float, breaker: CircuitBreaker
) -> tuple[str, int | None, Exception | None]:
    breaker.wait_if_tripped()
    try:
        df = fetch_game_pbp(game_id)
        df.to_parquet(out_dir / f"{game_id}.parquet", index=False)
        breaker.record_success()
        return game_id, len(df), None
    except Exception as exc:
        breaker.record_failure()
        return game_id, None, exc
    finally:
        time.sleep(sleep_seconds)


def fetch_and_save_season_pbp(
    season: str,
    breaker: CircuitBreaker,
    sleep_seconds: float = 0.5,
    workers: int = 4,
    force: bool = False,
    limit: int | None = None,
) -> None:
    game_ids = unique_game_ids(season)
    if limit:
        game_ids = game_ids[:limit]
    out_dir = RAW_DIR / season / "pbp"
    out_dir.mkdir(parents=True, exist_ok=True)

    todo = [gid for gid in game_ids if force or not (out_dir / f"{gid}.parquet").exists()]
    _log(f"[{season}] {len(game_ids)} games total, {len(todo)} to fetch ({workers} workers)")

    failed = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, gid, out_dir, sleep_seconds, breaker): gid for gid in todo}
        for future in as_completed(futures):
            game_id, n_events, exc = future.result()
            done += 1
            if exc is None:
                _log(f"[{season} {done}/{len(todo)}] {game_id}: {n_events} events")
            else:
                _log(f"[{season} {done}/{len(todo)}] {game_id}: FAILED ({exc})")
                failed.append(game_id)

    if failed:
        fail_path = out_dir.parent / "pbp_failed.txt"
        fail_path.write_text("\n".join(failed))
        _log(f"[{season}] {len(failed)} games failed after retries -> {fail_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seasons", nargs="+", help="e.g. 2025-26 2024-25")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="cap games per season (testing)")
    parser.add_argument("--sleep", type=float, default=0.5, help="per-thread pause between its own requests")
    parser.add_argument("--workers", type=int, default=4, help="concurrent requests in flight")
    parser.add_argument("--failure-threshold", type=int, default=5, help="consecutive failures before cooldown")
    parser.add_argument("--cooldown", type=float, default=90.0, help="seconds to pause once tripped")
    args = parser.parse_args()

    breaker = CircuitBreaker(failure_threshold=args.failure_threshold, cooldown_seconds=args.cooldown)

    for season in args.seasons:
        try:
            fetch_and_save_season_pbp(
                season,
                breaker,
                sleep_seconds=args.sleep,
                workers=args.workers,
                force=args.force,
                limit=args.limit,
            )
        except Exception as exc:
            print(f"[FAILED] {season}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()

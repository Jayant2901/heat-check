import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
REPLAY_FIXTURES_DIR = DATA_DIR / "replay_fixtures"


def _flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    # Pace of a REPLAY: one snapshot per poll_interval_seconds / speed. (The
    # live poll interval is live_poll_interval_seconds below.)
    poll_interval_seconds: float = float(os.getenv("POLL_INTERVAL_SECONDS", "12"))
    # How often one tracked live game is polled while the clock is running.
    # Scheduled against a fixed cadence (fetch time does not add to it), and
    # conditional requests make an unchanged feed cost a 304.
    live_poll_interval_seconds: float = float(os.getenv("LIVE_POLL_INTERVAL_SECONDS", "3"))
    # Used when the feed has been quiet (halftime, between periods, long
    # timeouts) so an idle game is not hammered; speeds back up on new plays.
    live_idle_poll_seconds: float = float(os.getenv("LIVE_IDLE_POLL_SECONDS", "10"))
    live_deep_idle_poll_seconds: float = float(os.getenv("LIVE_DEEP_IDLE_POLL_SECONDS", "15"))
    # The boxscore is only needed for game status, so it is refreshed less often than play-by-play.
    boxscore_refresh_seconds: float = float(os.getenv("BOXSCORE_REFRESH_SECONDS", "10"))
    poll_backoff_max_seconds: float = float(os.getenv("POLL_BACKOFF_MAX_SECONDS", "60"))
    # 1.5 was picked by checking the top run z-scores across all three
    # curated replay fixtures: the two ordinary games top out at 0.45-0.58,
    # while the "30-point comeback" game has three runs at 1.5-1.55 -- so
    # 1.5 is the highest threshold that still lets the comeback game's heat
    # checks fire without flagging anything in a normal game.
    #
    # Caveat: that is a tuning on 3 games, sampled at replay cadence (~3s),
    # not an evaluation. At live cadence (12s) the same games give different
    # flags -- see test_sampling_rate_changes_what_gets_flagged. Don't change
    # this without a proper evaluation over many games at live cadence.
    anomaly_z_threshold: float = float(os.getenv("ANOMALY_Z_THRESHOLD", "1.5"))
    # Below this, a "run" is really just a pair of free throws (clock frozen,
    # near-zero elapsed time), not a basketball "run" in any meaningful
    # sense -- confirmed directly: without this floor, ~every 2-3 point
    # exchange got flagged as a multi-sigma outlier.
    anomaly_min_magnitude: int = int(os.getenv("ANOMALY_MIN_MAGNITUDE", "6"))
    # Must stay <= 15 s so proxies don't close an idle stream.
    sse_heartbeat_seconds: float = float(os.getenv("SSE_HEARTBEAT_SECONDS", "10"))
    # How long a browser waits before reconnecting a dropped stream.
    sse_retry_ms: int = int(os.getenv("SSE_RETRY_MS", "3000"))
    # Finished (and idle-stopped) live games stay in memory so a late visitor
    # still gets the full chart and every heat check. Bounded on both axes.
    state_cache_max_games: int = int(os.getenv("STATE_CACHE_MAX_GAMES", "20"))
    state_cache_max_age_seconds: float = float(os.getenv("STATE_CACHE_MAX_AGE_SECONDS", str(24 * 3600)))
    # Abuse limits (per client address / whole process).
    max_streams_per_ip: int = int(os.getenv("MAX_STREAMS_PER_IP", "30"))
    max_total_streams: int = int(os.getenv("MAX_TOTAL_STREAMS", "500"))
    replay_starts_per_minute: int = int(os.getenv("REPLAY_STARTS_PER_MINUTE", "10"))
    # Comma-separated origins allowed to call the API cross-origin. Empty
    # (the default) means same-origin only: the bundled frontend needs no CORS.
    cors_allow_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    log_format: str = os.getenv("LOG_FORMAT", "text").strip().lower()  # "text" or "json"
    # Each replay session is an unauthenticated, free-to-spam way to spin up
    # a PollerManager task + GameState that lives until the replay finishes
    # (see poller.py's cleanup) -- without a cap, repeatedly hitting
    # POST /api/replays/{id}/start is a cheap resource-exhaustion vector on
    # a public demo URL.
    max_concurrent_replay_sessions: int = int(os.getenv("MAX_CONCURRENT_REPLAY_SESSIONS", "10"))
    # "live" reads cdn.nba.com; "demo" serves generated sample data (see
    # app/nba/demo.py) so the UI can be built and shown offline.
    data_mode: str = os.getenv("HEAT_CHECK_DATA", "live").strip().lower()
    # "auto": cdn.nba.com, falling back to ESPN if NBA.com refuses (it
    # answers 403 to some cloud hosts). "nba" or "espn" pins one source.
    nba_source: str = os.getenv("NBA_SOURCE", "auto").strip().lower()
    # Each live game being watched costs one poll every poll_interval_seconds
    # against NBA.com; this caps how many can run at once (429 beyond it).
    max_concurrent_live_games: int = int(os.getenv("MAX_CONCURRENT_LIVE_GAMES", "6"))
    # A live poller with nobody subscribed for this long stops itself.
    live_idle_stop_seconds: float = float(os.getenv("LIVE_IDLE_STOP_SECONDS", "120"))
    weekly_window_days: int = int(os.getenv("WEEKLY_WINDOW_DAYS", "7"))
    # Start building the weekly dashboard when the server starts.
    prewarm_week: bool = _flag("PREWARM_WEEK", "1")
    weekly_refresh_seconds: float = float(os.getenv("WEEKLY_REFRESH_SECONDS", "600"))
    # Minimum z-score for a run to appear in "Hottest runs" on the dashboard.
    # Higher than anomaly_z_threshold on purpose: the dashboard ranks a whole
    # week of games, so it can afford to only show the clearly unusual ones.
    weekly_run_min_z: float = float(os.getenv("WEEKLY_RUN_MIN_Z", "2.0"))
    # Tried first on cdn.nba.com requests: Referer/Origin of nba.com. Unverified
    # from a cloud host -- see README. Set NBA_BROWSER_HEADERS=0 to drop them.
    nba_browser_headers: bool = _flag("NBA_BROWSER_HEADERS", "1")
    win_prob_model_path: Path = ARTIFACTS_DIR / "win_prob_model_v1.joblib"
    run_baseline_path: Path = ARTIFACTS_DIR / "run_baseline_v1.parquet"


settings = Settings()

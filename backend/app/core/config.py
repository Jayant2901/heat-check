import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
REPLAY_FIXTURES_DIR = DATA_DIR / "replay_fixtures"


@dataclass(frozen=True)
class Settings:
    poll_interval_seconds: float = float(os.getenv("POLL_INTERVAL_SECONDS", "12"))
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
    sse_heartbeat_seconds: float = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))
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
    weekly_refresh_seconds: float = float(os.getenv("WEEKLY_REFRESH_SECONDS", "600"))
    # Minimum z-score for a run to appear in "Hottest runs" on the dashboard.
    # Higher than anomaly_z_threshold on purpose: the dashboard ranks a whole
    # week of games, so it can afford to only show the clearly unusual ones.
    weekly_run_min_z: float = float(os.getenv("WEEKLY_RUN_MIN_Z", "2.0"))
    win_prob_model_path: Path = ARTIFACTS_DIR / "win_prob_model_v1.joblib"
    run_baseline_path: Path = ARTIFACTS_DIR / "run_baseline_v1.parquet"


settings = Settings()

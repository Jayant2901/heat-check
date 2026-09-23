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
    win_prob_model_path: Path = ARTIFACTS_DIR / "win_prob_model_v1.joblib"
    run_baseline_path: Path = ARTIFACTS_DIR / "run_baseline_v1.parquet"


settings = Settings()

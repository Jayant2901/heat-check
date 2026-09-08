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
    anomaly_z_threshold: float = float(os.getenv("ANOMALY_Z_THRESHOLD", "2.5"))
    # Below this, a "run" is really just a pair of free throws (clock frozen,
    # near-zero elapsed time), not a basketball "run" in any meaningful
    # sense -- confirmed directly: without this floor, ~every 2-3 point
    # exchange got flagged as a multi-sigma outlier.
    anomaly_min_magnitude: int = int(os.getenv("ANOMALY_MIN_MAGNITUDE", "6"))
    sse_heartbeat_seconds: float = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))
    win_prob_model_path: Path = ARTIFACTS_DIR / "win_prob_model_v1.joblib"
    run_baseline_path: Path = ARTIFACTS_DIR / "run_baseline_v1.parquet"


settings = Settings()

"""The weekly dashboard and the model numbers shown under "How the numbers work"."""

import json
import logging

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.nba.weekly import get_weekly_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard/week")
async def get_week():
    """{"status": "building", "progress"} while the first build runs, then
    the ready payload (see app/nba/weekly.py). Never blocks on NBA.com."""
    try:
        service = get_weekly_service()
    except FileNotFoundError as exc:
        return {"status": "error", "message": str(exc)}
    return service.snapshot()


def _read_meta(path) -> dict:
    meta_path = path.with_suffix(".meta.json")
    return json.loads(meta_path.read_text())


@router.get("/model-info")
def get_model_info():
    """Held-out numbers straight from the training/baseline metadata files."""
    try:
        model_meta = _read_meta(settings.win_prob_model_path)
        baseline_meta = _read_meta(settings.run_baseline_path)
    except (OSError, ValueError):
        logger.warning("Model metadata missing", exc_info=True)
        raise HTTPException(status_code=503, detail="Model metadata is not available")

    chosen = model_meta.get("chosen_model")
    candidates = model_meta.get("candidates") or {}
    scores = candidates.get(chosen) or {}
    logistic = candidates.get("logistic_regression") or {}
    return {
        "model": chosen,
        "test_season": model_meta.get("test_season"),
        "train_seasons": model_meta.get("train_seasons"),
        "accuracy": scores.get("accuracy"),
        "log_loss": scores.get("log_loss"),
        "brier": scores.get("brier"),
        "logistic_log_loss": logistic.get("log_loss"),
        "logistic_accuracy": logistic.get("accuracy"),
        "n_test_rows": model_meta.get("n_test_rows"),
        "features": model_meta.get("feature_columns"),
        "n_runs": baseline_meta.get("n_runs"),
        "seasons": baseline_meta.get("seasons"),
        "anomaly_z_threshold": settings.anomaly_z_threshold,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "weekly_run_min_z": settings.weekly_run_min_z,
    }

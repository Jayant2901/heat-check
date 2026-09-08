"""Shared parsing for nba_api's raw play-by-play rows.

Used by build_replay_fixture.py, build_dataset.py, and build_baseline.py --
all three walk the same raw pbp parquet files and need the same two
conversions (clock string -> seconds, score cell -> int-or-missing).
"""

import re

_CLOCK_RE = re.compile(r"PT(?:(\d+)M)?(?:([\d.]+)S)?")


def parse_clock_seconds(iso_clock: str) -> float:
    """nba_api's gameClock/clock field, e.g. 'PT08M45.00S' -> 525.0."""
    match = _CLOCK_RE.match(iso_clock or "")
    if not match:
        return 0.0
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return minutes * 60 + seconds


def parse_score_value(raw) -> int | None:
    """scoreHome/scoreAway are blank except on rows where the score changed."""
    text = str(raw).strip()
    if text in ("", "None", "nan", "<NA>"):
        return None
    return int(text)

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class WPUpdateEvent:
    type: Literal["wp_update"]
    t: float
    period: int
    clock: float
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    wp_home: float
    wp_away: float
    # "scheduled" | "live" | "final" -- lets the game page tell a pre-game
    # snapshot from a live one.
    status: str = "live"
    # The scoring run in progress right now, or None. Sent on EVERY update so
    # the page can show run state even when nothing has crossed the heat-check
    # threshold. Keys: team ("home"/"away"), points, duration_sec (first to
    # latest basket), since_last_score_sec, z_score (None until the run is big
    # enough to score), threshold, flagged.
    run: dict | None = None
    # "play_by_play" (exact scoring times) or "boxscore_ticks" (approximate).
    fidelity: str = "play_by_play"
    # Epoch seconds: when the feed says the play happened, and when this
    # server emitted the event. feed_delay_sec = server_ts - source_ts, only
    # set for plays seen live (None while backfilling history and in replays).
    source_ts: float | None = None
    server_ts: float | None = None
    feed_delay_sec: float | None = None
    # Position in this game's event log; see api/stream.py (Last-Event-ID).
    seq: int = 0


@dataclass(frozen=True)
class AnomalyEvent:
    type: Literal["anomaly"]
    t: float
    team: str
    magnitude: int
    duration_sec: float
    z_score: float
    bucket: str
    message: str
    period: int = 0
    clock: float = 0.0
    team_code: str = ""
    # "mild" | "mid" | "high" | "extreme" -- the plain-language rarity tier.
    tier: str = "mild"
    source_ts: float | None = None
    server_ts: float | None = None
    seq: int = 0


@dataclass(frozen=True)
class GameEndEvent:
    type: Literal["game_end"]
    final_home_score: int
    final_away_score: int
    winner: str
    seq: int = 0


@dataclass(frozen=True)
class DegradedEvent:
    type: Literal["degraded"]
    reason: str
    seq: int = 0


SSEEvent = WPUpdateEvent | AnomalyEvent | GameEndEvent | DegradedEvent


def to_sse_payload(event: SSEEvent) -> dict:
    return asdict(event)

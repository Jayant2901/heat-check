from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class WPUpdateEvent:
    type: Literal["wp_update"]
    t: float
    period: int
    clock: float
    home_score: int
    away_score: int
    wp_home: float
    wp_away: float


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


@dataclass(frozen=True)
class GameEndEvent:
    type: Literal["game_end"]
    final_home_score: int
    final_away_score: int
    winner: str


@dataclass(frozen=True)
class DegradedEvent:
    type: Literal["degraded"]
    reason: str


SSEEvent = WPUpdateEvent | AnomalyEvent | GameEndEvent | DegradedEvent


def to_sse_payload(event: SSEEvent) -> dict:
    return asdict(event)

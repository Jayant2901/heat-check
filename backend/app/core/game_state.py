import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.interfaces import FIDELITY_PLAY_BY_PLAY


@dataclass
class ScoreHistoryPoint:
    elapsed_seconds: float
    home_score: int
    away_score: int
    wp_home: float


@dataclass
class RunState:
    team: str
    start_elapsed_seconds: float
    start_score_diff: int
    points_scored: int = 0
    last_scored_elapsed: float = 0.0
    anomaly_flagged: bool = False
    # Where the run STARTED. The run baseline buckets a run by its start
    # (backend/historical/build_baseline.py), so live z-scores must too.
    start_period: int = 0
    start_clock: float = 0.0
    # z-score as of the run's latest basket (None until it can be scored).
    # It only changes when the run scores, so it is cached here and sent
    # with every update.
    z_score: float | None = None


@dataclass
class AnomalyEvent:
    elapsed_seconds: float
    team: str
    magnitude: int
    duration_seconds: float
    z_score: float
    bucket: str
    message: str


@dataclass
class GameState:
    game_id: str
    status: str = "scheduled"
    period: int = 0
    clock_seconds_remaining: float = 0.0
    home_team: str = ""
    away_team: str = ""
    home_score: int = 0
    away_score: int = 0
    score_history: list[ScoreHistoryPoint] = field(default_factory=list)
    current_run: RunState | None = None
    anomalies: list[AnomalyEvent] = field(default_factory=list)
    model_version: str | None = None
    baseline_version: str | None = None
    fidelity: str = FIDELITY_PLAY_BY_PLAY
    # Every wp_update / anomaly / game_end emitted so far, in order, each with
    # its seq. A late viewer's backlog is this log, so it matches exactly what
    # a viewer who was there from the start received.
    log: list = field(default_factory=list)
    seq: int = 0
    # Highest play-by-play action number applied. A play delivered twice (a
    # retried poll) is ignored, so the state is the same however plays arrive.
    last_action_number: int | None = None


class GameStateStore:
    """In-memory per-game state, keyed by game_id.

    v1 is a single-process dict. Scaling to multiple concurrent games only
    ever means more entries in this dict, one poller task per key -- see
    backend/app/polling/poller.py. Scaling beyond one process means
    replacing this with a Redis-backed store; nothing above this class
    should need to change to make that swap.

    A state whose poller has stopped is not deleted straight away: retain()
    parks it in a bounded cache (at most `max_retained` games, none older
    than `max_age_seconds`) so a visitor who arrives after the game ended, or
    after an idle poller shut down, still gets the full chart and every heat
    check. Active states are never evicted.
    """

    def __init__(self, max_retained: int | None = None, max_age_seconds: float | None = None, clock=time.monotonic) -> None:
        self._games: dict[str, GameState] = {}
        self._retained: OrderedDict[str, float] = OrderedDict()
        self._max_retained = settings.state_cache_max_games if max_retained is None else max_retained
        self._max_age = settings.state_cache_max_age_seconds if max_age_seconds is None else max_age_seconds
        self._clock = clock

    def get(self, game_id: str) -> GameState | None:
        self._evict()
        return self._games.get(game_id)

    def get_or_create(self, game_id: str) -> GameState:
        if game_id not in self._games:
            self._games[game_id] = GameState(game_id=game_id)
        return self._games[game_id]

    def reset(self, game_id: str) -> GameState:
        """Start the key over with an empty state (a poller that rebuilds the
        whole game from the feed must not stack it on a stale copy)."""
        self._retained.pop(game_id, None)
        self._games[game_id] = GameState(game_id=game_id)
        return self._games[game_id]

    def activate(self, game_id: str) -> None:
        """A poller is running for this key: it is no longer evictable."""
        self._retained.pop(game_id, None)

    def retain(self, game_id: str) -> None:
        """The poller stopped. Keep the state, within the cache bounds."""
        if game_id not in self._games:
            return
        self._retained[game_id] = self._clock()
        self._retained.move_to_end(game_id)
        self._evict()

    def is_retained(self, game_id: str) -> bool:
        return game_id in self._retained

    def remove(self, game_id: str) -> None:
        self._games.pop(game_id, None)
        self._retained.pop(game_id, None)

    def all_ids(self) -> list[str]:
        return list(self._games.keys())

    def _evict(self) -> None:
        now = self._clock()
        for key, stored in list(self._retained.items()):
            if now - stored > self._max_age:
                self.remove(key)
        while len(self._retained) > self._max_retained:
            oldest = next(iter(self._retained))
            self.remove(oldest)


game_state_store = GameStateStore()

from dataclasses import dataclass, field


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


class GameStateStore:
    """In-memory per-game state, keyed by game_id.

    v1 is a single-process dict. Scaling to multiple concurrent games only
    ever means more entries in this dict, one poller task per key -- see
    backend/app/polling/poller.py. Scaling beyond one process means
    replacing this with a Redis-backed store; nothing above this class
    should need to change to make that swap.
    """

    def __init__(self) -> None:
        self._games: dict[str, GameState] = {}

    def get(self, game_id: str) -> GameState | None:
        return self._games.get(game_id)

    def get_or_create(self, game_id: str) -> GameState:
        if game_id not in self._games:
            self._games[game_id] = GameState(game_id=game_id)
        return self._games[game_id]

    def remove(self, game_id: str) -> None:
        self._games.pop(game_id, None)

    def all_ids(self) -> list[str]:
        return list(self._games.keys())


game_state_store = GameStateStore()

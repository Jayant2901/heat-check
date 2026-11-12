from app.core.game_state import GameStateStore


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_a_retained_game_survives_and_is_found_again():
    store = GameStateStore(max_retained=5, max_age_seconds=100)
    store.get_or_create("g1").home_score = 99
    store.retain("g1")  # its poller stopped
    assert store.get("g1").home_score == 99
    assert store.is_retained("g1")


def test_retained_games_are_bounded_by_count_oldest_first():
    store = GameStateStore(max_retained=3, max_age_seconds=1000)
    for n in range(6):
        store.get_or_create(f"g{n}")
        store.retain(f"g{n}")
    assert sorted(store.all_ids()) == ["g3", "g4", "g5"]


def test_retained_games_are_bounded_by_age():
    clock = Clock()
    store = GameStateStore(max_retained=50, max_age_seconds=100, clock=clock)
    store.get_or_create("old")
    store.retain("old")
    clock.t = 60
    store.get_or_create("newer")
    store.retain("newer")
    clock.t = 120  # "old" is 120 s old, "newer" 60 s
    assert store.get("old") is None
    assert store.get("newer") is not None


def test_active_games_are_never_evicted():
    clock = Clock()
    store = GameStateStore(max_retained=1, max_age_seconds=10, clock=clock)
    store.get_or_create("live")  # has a poller: never retained
    for n in range(4):
        store.get_or_create(f"done{n}")
        store.retain(f"done{n}")
    clock.t = 1000
    assert store.get("live") is not None
    assert store.get("done3") is None


def test_activating_a_retained_game_takes_it_out_of_the_eviction_pool():
    store = GameStateStore(max_retained=1, max_age_seconds=100)
    store.get_or_create("a")
    store.retain("a")
    store.activate("a")  # a new poller took over
    store.get_or_create("b")
    store.retain("b")
    store.get_or_create("c")
    store.retain("c")
    assert store.get("a") is not None


def test_reset_starts_over():
    store = GameStateStore()
    old = store.get_or_create("a")
    old.home_score = 5
    store.retain("a")
    fresh = store.reset("a")
    assert fresh is not old and fresh.home_score == 0 and not store.is_retained("a")


def test_retaining_an_unknown_game_is_a_no_op():
    store = GameStateStore()
    store.retain("ghost")
    assert store.all_ids() == []

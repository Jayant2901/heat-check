from app.model.features import elapsed_seconds, time_remaining_seconds


def test_elapsed_seconds_start_of_game():
    assert elapsed_seconds(period=1, clock_seconds_remaining=12 * 60) == 0


def test_elapsed_seconds_end_of_regulation():
    assert elapsed_seconds(period=4, clock_seconds_remaining=0) == 4 * 12 * 60


def test_elapsed_seconds_into_first_ot():
    # end of regulation (2880s) + 2 minutes into a 5-minute OT
    assert elapsed_seconds(period=5, clock_seconds_remaining=3 * 60) == 4 * 12 * 60 + 2 * 60


def test_time_remaining_start_of_game():
    assert time_remaining_seconds(period=1, clock_seconds_remaining=12 * 60) == 4 * 12 * 60


def test_time_remaining_end_of_regulation():
    assert time_remaining_seconds(period=4, clock_seconds_remaining=0) == 0


def test_time_remaining_during_ot_is_just_the_ot_clock():
    assert time_remaining_seconds(period=5, clock_seconds_remaining=90) == 90

"""What the agent keeps while the hub is not listening, and what it gives up first."""

import pytest

from sentry_satellite.spool import Spool


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


def spool(clock, **changes) -> Spool:
    settings = {"max_count": 4, "max_bytes": 1000, "max_age_seconds": 60, "clock": clock}
    return Spool(**{**settings, **changes})


def test_what_goes_in_comes_out_in_order(clock):
    queue = spool(clock)
    for number in range(3):
        queue.put("events", {"n": number}, 10)
    assert [held.item["n"] for held in queue.take(3)] == [0, 1, 2]
    assert len(queue) == 0
    assert queue.nbytes == 0


def test_the_oldest_is_the_first_to_go_when_there_are_too_many(clock):
    queue = spool(clock)
    for number in range(6):
        queue.put("events", {"n": number}, 10)
    assert [held.item["n"] for held in queue.take(10)] == [2, 3, 4, 5]
    assert queue.drops.count == 2
    assert queue.drops.total == 2


def test_a_queue_is_bounded_by_size_as_well_as_by_count(clock):
    queue = spool(clock, max_bytes=100)
    for number in range(5):
        queue.put("events", {"n": number}, 40)
    assert queue.nbytes <= 100
    assert queue.drops.bytes > 0


def test_a_reading_nobody_collected_in_time_is_not_worth_sending(clock):
    queue = spool(clock, max_age_seconds=30)
    queue.put("events", {"n": 0}, 10)
    clock.advance(31)
    queue.put("events", {"n": 1}, 10)
    assert [held.item["n"] for held in queue.take(10)] == [1]
    assert queue.drops.age == 1


def test_something_that_could_not_be_sent_keeps_its_place(clock):
    queue = spool(clock)
    queue.put("events", {"n": 0}, 10)
    queue.put("events", {"n": 1}, 10)
    first = queue.take(1)[0]
    queue.requeue(first)
    assert [held.item["n"] for held in queue.take(2)] == [0, 1]


def test_requeueing_into_a_full_queue_drops_the_newest_not_the_thing_being_put_back(clock):
    queue = spool(clock, max_count=2)
    for number in range(2):
        queue.put("events", {"n": number}, 10)
    held = queue.take(1)[0]
    queue.put("events", {"n": 99}, 10)
    queue.requeue(held)
    assert [item.item["n"] for item in queue.take(5)] == [0, 1]


def test_how_long_something_waited_is_measured_not_guessed(clock):
    queue = spool(clock)
    queue.put("events", {"n": 0}, 10)
    clock.advance(2.5)
    held = queue.take(1)[0]
    assert queue.queued_ms(held) == 2500

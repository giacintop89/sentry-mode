"""The picture of a node right now: what it says about itself, and the one thing the hub
measures about it from the other end."""

from sentry_mode.satellites.health import BEHIND_MS, WORST_FOR_SECONDS, HealthBoard


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_a_node_that_has_never_sent_a_reading_has_no_wait_to_report():
    board = HealthBoard(clock=Clock())
    board.heartbeat("pico-cablato", {"clock_status": "synced"})
    waited = board.of("pico-cablato")["waited"]
    assert waited["last_ms"] is None and waited["worst_ms"] is None
    assert waited["behind"] is False


def test_the_worst_wait_is_remembered_and_then_let_go_of():
    clock = Clock()
    board = HealthBoard(clock=clock)
    board.waited("pico-cablato", 5)
    board.waited("pico-cablato", 800)
    board.waited("pico-cablato", 6)
    waited = board.of("pico-cablato")["waited"]
    # The last one is the last one; the worst is the worst, and it is not the last.
    assert (waited["last_ms"], waited["worst_ms"]) == (6, 800)

    # Five minutes later that 800 is history, not a picture of now.
    clock.now += WORST_FOR_SECONDS + 1
    board.waited("pico-cablato", 7)
    waited = board.of("pico-cablato")["waited"]
    assert (waited["last_ms"], waited["worst_ms"]) == (7, 7)


def test_a_node_falling_behind_is_said_once_and_so_is_its_coming_back(caplog):
    board = HealthBoard(clock=Clock())
    with caplog.at_level("INFO", logger="sentry_mode.satellites.health"):
        board.waited("pico-cablato", 5)
        board.waited("pico-cablato", BEHIND_MS + 1)
        board.waited("pico-cablato", BEHIND_MS + 9000)
        assert board.of("pico-cablato")["waited"]["behind"] is True
        board.waited("pico-cablato", 5)
        assert board.of("pico-cablato")["waited"]["behind"] is False
    said = [record.getMessage() for record in caplog.records]
    # Twice, not five times: an operator reads a line that changed something.
    assert sum("falling behind" in line for line in said) == 1
    assert sum("caught up" in line for line in said) == 1


def test_a_wait_nobody_measured_is_not_a_wait_of_zero():
    board = HealthBoard(clock=Clock())
    board.waited("pico-cablato", 40)
    board.waited("pico-cablato", None)
    assert board.of("pico-cablato")["waited"]["last_ms"] == 40

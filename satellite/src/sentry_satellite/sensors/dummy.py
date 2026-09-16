"""Drivers with no hardware behind them, for the simulator and for the tests.

A real GPIO driver arrives with PR-06, once there is a sensor wired to a real pin. Until
then these are what proves the rest of the agent works: they produce readings on demand,
and one of them deliberately never produces anything at all.
"""

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from threading import Event
from typing import Callable

from sentry_satellite.sensors import Reading


class Scripted:
    """Reports exactly what it was given, then waits to be stopped."""

    def __init__(
        self,
        source_id: str,
        readings: Iterable[Reading],
        *,
        interval: float = 0.0,
    ) -> None:
        self.source_id = source_id
        self._readings = list(readings)
        self._interval = interval
        self._stopped = Event()

    def read(self) -> Iterator[Reading]:
        for reading in self._readings:
            if self._stopped.is_set():
                return
            yield reading
            if self._interval and self._stopped.wait(self._interval):
                return
        self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()


class Motion:
    """A pretend passive infrared sensor that alternates between seeing and not seeing."""

    def __init__(
        self,
        source_id: str,
        *,
        interval: float = 1.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source_id = source_id
        self._interval = interval
        self._now = now
        self._stopped = Event()

    def read(self) -> Iterator[Reading]:
        detected = False
        while not self._stopped.is_set():
            detected = not detected
            yield Reading(
                source_id=self.source_id,
                kind="sensor.motion",
                value=detected,
                occurred_at=self._now(),
            )
            if self._stopped.wait(self._interval):
                return

    def stop(self) -> None:
        self._stopped.set()


class Stalled:
    """Never reports anything and never notices that it has been asked to stop.

    This is what a sensor looks like when its bus hangs. It exists so that the tests can
    prove the agent keeps its promises around a driver that has stopped keeping its own.
    """

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self._forever = Event()

    def read(self) -> Iterator[Reading]:
        self._forever.wait()
        return
        yield  # pragma: no cover - unreachable, and that is the point

    def stop(self) -> None:
        return

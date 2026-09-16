"""Sensors that are asked for a value on a schedule: temperature, humidity, light.

Each one reports every `interval_seconds`. The first good value is marked as the
baseline. A read that fails, times out or returns something impossible is reported as
`unavailable` with no value: never a zero, never the last good value again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Event

from sentry_satellite.sensors import Reading
from sentry_satellite.sensors.bounded import Bounded, Busy, Timeout

log = logging.getLogger(__name__)


class Unreadable(RuntimeError):
    """The sensor answered, but not with a value worth reporting."""


@dataclass(frozen=True)
class Sample:
    value: float
    quality: str = "valid"


class Periodic:
    def __init__(
        self,
        source_id: str,
        sample: Callable[[], Sample],
        *,
        event_kind: str,
        unit: str | None,
        interval_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source_id = source_id
        self._sample = sample
        self._kind = event_kind
        self._unit = unit
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._now = now
        self._stopped = Event()
        self._bounded: Bounded[Sample] = Bounded(source_id)
        self._last: Reading | None = None
        self.error: str | None = None

    def current(self) -> Reading | None:
        last = self._last
        if last is None or last.quality != "valid":
            return None
        return replace(last, initial=True)

    def read(self) -> Iterator[Reading]:
        baseline_sent = False
        while not self._stopped.is_set():
            reading = self._once()
            good = reading.quality == "valid"
            if good and not baseline_sent:
                reading = replace(reading, initial=True)
                baseline_sent = True
            # A failure is said once; repeating it every interval tells the hub nothing.
            if good or self._last is None or self._last.quality == "valid":
                self._last = reading
                yield reading
            if self._stopped.wait(self._interval):
                return

    def stop(self) -> None:
        self._stopped.set()

    def _once(self) -> Reading:
        taken = self._now()
        try:
            sample = self._bounded.call(self._sample, self._timeout)
        except (Timeout, Busy, Unreadable, OSError) as error:
            self.error = str(error)
        except Exception as error:  # noqa: BLE001 - a driver bug is still not a value
            log.exception("%s: the read failed unexpectedly", self.source_id)
            self.error = f"{type(error).__name__}: {error}"
        else:
            self.error = None
            return Reading(
                source_id=self.source_id,
                kind=self._kind,
                value=sample.value,
                occurred_at=taken,
                unit=self._unit,
                quality=sample.quality,
            )
        return Reading(
            source_id=self.source_id,
            kind=self._kind,
            value=None,
            occurred_at=taken,
            unit=self._unit,
            quality="unavailable",
        )

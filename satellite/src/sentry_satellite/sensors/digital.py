"""A digital input: a PIR, a door contact, a button.

What the driver promises:

- **Edges, not samples.** It reports when the debounced state changes, and nothing in
  between.
- **A baseline first, marked as one.** After the line is opened and the settle time has
  passed, the current state is reported with `initial` set. The hub records it and does
  not treat it as news, so a restart never looks like a door opening.
- **Debounced here.** After an edge the driver waits `debounce_ms` and reads the level
  once more. It reports only if the level differs from the last one it reported. Bounce
  that ends where it started reports nothing. A pulse shorter than the debounce time is
  lost; a PIR holds its output for seconds, a door contact bounces for milliseconds.
- **Unknown is not false.** If the line cannot be opened or read, the driver reports
  `unavailable` with no value, then retries. When it gets the line back it reports a new
  baseline. A floating input is not detectable, so choose `bias` to suit the wiring.
- **The settle time is the module's, not a guess.** `settle_seconds` defaults to 0.
  Set it to the warm-up measured on the module that is actually fitted; edges during
  that time are ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from threading import Event

from sentry_satellite.sensors import Reading
from sentry_satellite.sensors.gpio import Line, LineError


class DigitalInput:
    def __init__(
        self,
        source_id: str,
        open_line: Callable[[], Line],
        *,
        event_kind: str = "sensor.motion",
        active_high: bool = True,
        debounce_ms: int = 50,
        settle_seconds: float = 0.0,
        retry_seconds: float = 5.0,
        poll_seconds: float = 0.5,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source_id = source_id
        self._open_line = open_line
        self._kind = event_kind
        self._active_high = active_high
        self._debounce = debounce_ms / 1000
        self._settle = settle_seconds
        self._retry = retry_seconds
        self._poll = poll_seconds
        self._now = now
        self._stopped = Event()
        self._state: bool | None = None
        self.error: str | None = None

    def current(self) -> Reading | None:
        """The state as last reported, again, as a baseline. None if it is not known."""
        state = self._state
        if state is None:
            return None
        return self._reading(state, initial=True)

    def read(self) -> Iterator[Reading]:
        while not self._stopped.is_set():
            try:
                line = self._open_line()
            except LineError as error:
                yield from self._lost(str(error))
                continue
            try:
                yield from self._watch(line)
            except LineError as error:
                yield from self._lost(str(error))
            finally:
                line.release()

    def stop(self) -> None:
        self._stopped.set()

    def _watch(self, line: Line) -> Iterator[Reading]:
        if self._settle and self._stopped.wait(self._settle):
            return
        line.edges()  # whatever happened while the sensor was warming up
        self._state = self._active(line.level())
        self.error = None
        yield self._reading(self._state, initial=True)
        while not self._stopped.is_set():
            if not line.wait(self._poll):
                continue
            seen = self._now()
            line.edges()
            if self._debounce and self._stopped.wait(self._debounce):
                return
            line.edges()
            state = self._active(line.level())
            if state != self._state:
                self._state = state
                yield self._reading(state, occurred_at=seen)

    def _lost(self, message: str) -> Iterator[Reading]:
        first = self.error is None
        self._state = None
        self.error = message
        if first:
            yield Reading(
                source_id=self.source_id,
                kind=self._kind,
                value=None,
                occurred_at=self._now(),
                quality="unavailable",
            )
        self._stopped.wait(self._retry)

    def _active(self, high: bool) -> bool:
        return high if self._active_high else not high

    def _reading(
        self, state: bool, *, initial: bool = False, occurred_at: datetime | None = None
    ) -> Reading:
        return Reading(
            source_id=self.source_id,
            kind=self._kind,
            value=state,
            occurred_at=occurred_at or self._now(),
            initial=initial,
        )

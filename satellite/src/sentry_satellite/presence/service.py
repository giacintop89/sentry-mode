"""Whether one device is here: present, absent, or unknown, and nothing in between.

Present takes `enter_sightings` sightings within `enter_window_seconds`. Sightings less
than `MIN_GAP_SECONDS` apart count once: a beacon that advertises ten times a second is
not ten times as present, and a burst from something passing by is one sighting.

Absent takes `absent_after_seconds` without a sighting while the scanner was scanning the
whole time. A scanner that stops, for any reason, makes the state unknown at once, and the
wait for absence starts again when it is back. Nothing is ever concluded from a gap in
what the node could hear.

A state reached from unknown is where things stand, not a change: it is reported as a
baseline, so an arrival rule does not fire because a node restarted next to a device that
was there all along.

The signal strength is kept, smoothed, for the operator to see. It is never turned into a
distance.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from threading import Event
from typing import Any

from sentry_satellite.presence.bluez import BluezScanner, Coverage, Matcher, Sighting
from sentry_satellite.sensors import Reading

MIN_GAP_SECONDS = 1.0
SMOOTHING = 0.3
UNKNOWN, PRESENT, ABSENT = "unknown", "present", "absent"


class Presence:
    def __init__(
        self,
        *,
        enter_sightings: int = 3,
        enter_window_seconds: float = 10.0,
        absent_after_seconds: float = 120.0,
        rssi_min: int = -100,
    ) -> None:
        self.enter_sightings = enter_sightings
        self.enter_window = enter_window_seconds
        self.absent_after = absent_after_seconds
        self.rssi_min = rssi_min
        self.state = UNKNOWN
        self.covered_since: float | None = None
        self.last_seen: float | None = None
        self.rssi: float | None = None
        self.sightings = 0
        self.weak = 0
        self._hits: deque[float] = deque()

    def coverage(self, scanning: bool, at: float) -> str | None:
        if scanning:
            if self.covered_since is None:
                self.covered_since = at
            return None
        self.covered_since = None
        self._hits.clear()
        return self._become(UNKNOWN)

    def seen(self, at: float, rssi: int | None) -> str | None:
        if self.covered_since is None:
            return None
        if rssi is not None and rssi < self.rssi_min:
            self.weak += 1
            return None
        self.sightings += 1
        self.last_seen = at
        if rssi is not None:
            self.rssi = rssi if self.rssi is None else self.rssi + SMOOTHING * (rssi - self.rssi)
        if self._hits and at - self._hits[-1] < MIN_GAP_SECONDS:
            return None
        self._hits.append(at)
        while self._hits and at - self._hits[0] > self.enter_window:
            self._hits.popleft()
        if self.state != PRESENT and len(self._hits) >= self.enter_sightings:
            return self._become(PRESENT)
        return None

    def tick(self, at: float) -> str | None:
        if self.covered_since is None or self.state == ABSENT:
            return None
        quiet_since = max(self.covered_since, self.last_seen or self.covered_since)
        if at - quiet_since >= self.absent_after:
            self._hits.clear()
            return self._become(ABSENT)
        return None

    def _become(self, state: str) -> str | None:
        if state == self.state:
            return None
        self.state = state
        return state


class BlePresence:
    """A `ble` source: one device, watched through the board's shared scanner."""

    def __init__(
        self,
        source_id: str,
        options: Mapping[str, Any],
        *,
        scanner: BluezScanner,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        tick_seconds: float = 1.0,
    ) -> None:
        self.source_id = source_id
        self.kind = options.get("event_kind", "presence.state")
        self.scanner = scanner
        self.matcher = Matcher(
            address=options.get("address") or None,
            ibeacon_uuid=options.get("ibeacon_uuid") or None,
            major=_any(options.get("ibeacon_major", -1)),
            minor=_any(options.get("ibeacon_minor", -1)),
        )
        self.tracker = Presence(
            enter_sightings=options.get("enter_sightings", 3),
            enter_window_seconds=options.get("enter_window_seconds", 10.0),
            absent_after_seconds=options.get("absent_after_seconds", 120.0),
            rssi_min=options.get("rssi_min", -100),
        )
        self._clock = clock
        self._now = now
        self._tick = tick_seconds
        self._stopped = Event()
        self.error: str | None = None

    def read(self) -> Iterator[Reading]:
        watch = self.scanner.subscribe(self.matcher)
        try:
            while not self._stopped.is_set():
                news = watch.get(self._tick)
                before = self.tracker.state
                if isinstance(news, Coverage):
                    self.error = None if news.scanning else news.reason
                    changed = self.tracker.coverage(news.scanning, news.at)
                elif isinstance(news, Sighting):
                    changed = self.tracker.seen(news.at, news.rssi)
                else:
                    changed = None
                changed = changed or self.tracker.tick(self._clock())
                if changed is not None and not self._stopped.is_set():
                    yield self._reading(changed, initial=before == UNKNOWN)
        finally:
            self.scanner.unsubscribe(watch)

    def _reading(self, state: str, *, initial: bool) -> Reading:
        if state == UNKNOWN:
            return Reading(self.source_id, self.kind, None, self._now(), quality="unknown")
        return Reading(self.source_id, self.kind, state, self._now(), initial=initial)

    def stop(self) -> None:
        self._stopped.set()

    def presence(self) -> dict:
        tracker = self.tracker
        now = self._clock()
        return {
            "state": tracker.state,
            "error": self.error,
            "rssi": None if tracker.rssi is None else round(tracker.rssi),
            "last_seen_seconds": None
            if tracker.last_seen is None
            else round(now - tracker.last_seen, 1),
            "sightings": tracker.sightings,
            "too_weak": tracker.weak,
            "scanner": self.scanner.status(),
        }


def _any(value: int) -> int | None:
    return None if value < 0 else value


__all__ = ["BlePresence", "Presence"]

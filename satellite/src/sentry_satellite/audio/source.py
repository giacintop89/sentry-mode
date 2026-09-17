"""The microphone as a source: silent unless the hub asks for sound or activity is wanted.

With `activity` off the microphone reports nothing and opens nothing; it waits for the hub
to ask for a stream. With `activity` on it keeps one capture running and reports
`audio.activity` when the level stays above `activity_threshold_dbfs` for
`activity_min_seconds`, and again, with false, once it has stayed
`HYSTERESIS_DB` below that for `activity_hold_seconds`. It says that something was loud,
never what it was: no sound leaves the node unless the hub asked for a stream.
"""

import array
import math
import operator
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from threading import Event
from typing import Any

from sentry_satellite.audio.blocks import CHANNELS, RATE, SAMPLE_BYTES
from sentry_satellite.audio.capture import Capture
from sentry_satellite.sensors import Reading
from sentry_satellite.subprocesses import Child

PROGRAM = "arecord"
HYSTERESIS_DB = 6.0
LEVEL_STRIDE = 4
"""A level is measured on 4 kHz of the 16: a quarter of the arithmetic."""
SILENCE_DBFS = -96.0


def capture_argv(options: Mapping[str, Any], *, program: str = PROGRAM) -> list[str]:
    """The one capture command, from checked options: raw 16 kHz mono to standard output."""
    return [
        program,
        "--quiet",
        "--device",
        str(options["device"]),
        "--file-type",
        "raw",
        "--format",
        "S16_LE",
        "--rate",
        str(RATE),
        "--channels",
        str(CHANNELS),
    ]


def dbfs(pcm: bytes) -> float:
    """The level of a block of signed 16-bit little-endian samples, in dB full scale.

    Every `LEVEL_STRIDE`th sample is enough to tell loud from quiet, and a loop in Python
    over every sample of a continuous capture is a cost a Zero W notices.
    """
    samples = array.array("h", pcm[: len(pcm) // SAMPLE_BYTES * SAMPLE_BYTES])
    if sys.byteorder == "big":
        samples.byteswap()
    samples = samples[::LEVEL_STRIDE]
    if not samples:
        return SILENCE_DBFS
    rms = math.sqrt(sum(map(operator.mul, samples, samples)) / len(samples))
    return max(SILENCE_DBFS, round(20 * math.log10(max(rms, 1e-9) / 32768), 1))


class Activity:
    """Loud for long enough starts it; quiet for long enough ends it. Nothing in between."""

    def __init__(self, threshold_dbfs: float, min_seconds: float, hold_seconds: float) -> None:
        self.threshold = threshold_dbfs
        self.min_seconds = min_seconds
        self.hold_seconds = hold_seconds
        self.active = False
        self._loud = 0.0
        self._quiet = 0.0

    def step(self, level: float, seconds: float) -> bool | None:
        """The new state when it changes, None otherwise."""
        if level >= self.threshold:
            self._loud += seconds
            self._quiet = 0.0
        elif level < self.threshold - HYSTERESIS_DB:
            self._quiet += seconds
            self._loud = 0.0
        else:
            self._loud = 0.0
        if not self.active and self._loud >= self.min_seconds:
            self.active = True
            return True
        if self.active and self._quiet >= self.hold_seconds:
            self.active = False
            return False
        return None


class AlsaMicrophone:
    def __init__(
        self,
        source_id: str,
        options: Mapping[str, Any],
        *,
        program: str | None = None,
        child: Callable[..., Child] = Child,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.source_id = source_id
        self.options = dict(options)
        self.program = program or shutil.which(PROGRAM)
        self.missing = (
            None if self.program else f"{PROGRAM} is not installed (apt install alsa-utils)"
        )
        self.capture = Capture(source_id, self.capture_argv, child=child)
        self._now = now
        self._stop = Event()
        self._kind = str(self.options.get("event_kind", "audio.activity"))
        self._activity = (
            Activity(
                float(self.options["activity_threshold_dbfs"]),
                float(self.options["activity_min_seconds"]),
                float(self.options["activity_hold_seconds"]),
            )
            if self.options.get("activity")
            else None
        )
        self.level_dbfs: float | None = None

    @property
    def error(self) -> str | None:
        return self.missing or self.capture.error

    def capture_argv(self) -> list[str]:
        if self.program is None:
            raise RuntimeError(self.missing)
        return capture_argv(self.options, program=self.program)

    def current(self) -> Reading | None:
        if self._activity is None:
            return None
        return self._reading(self._activity.active, initial=True)

    def read(self) -> Iterator[Reading]:
        if self._activity is None or self.missing:
            self._stop.wait()
            return
        tap = self.capture.subscribe()
        try:
            while not self._stop.is_set():
                chunk = tap.get(0.5)
                if chunk is None:
                    continue
                level = dbfs(chunk.pcm)
                self.level_dbfs = level
                seconds = len(chunk.pcm) / SAMPLE_BYTES / RATE
                change = self._activity.step(level, seconds)
                if change is not None:
                    yield self._reading(change)
        finally:
            self.capture.unsubscribe(tap)

    def _reading(self, active: bool, *, initial: bool = False) -> Reading:
        return Reading(
            source_id=self.source_id,
            kind=self._kind,
            value=active,
            occurred_at=self._now(),
            initial=initial,
        )

    def stop(self) -> None:
        self._stop.set()
        self.capture.stop()

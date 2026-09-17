"""What the board says about itself: how warm it is, and how busy it has been.

Every node has these two and neither needs a wire, so they are reported without being
configured. The temperature is the thermal zone the firmware exposes. The occupancy is
worked out from `/proc/stat` between one reading and the next, because "busy since you
last asked" is the only honest answer: the counters are totals since boot, and an
instantaneous figure taken from them would say nothing.
"""

from __future__ import annotations

from pathlib import Path

from sentry_satellite.sensors.periodic import Sample, Unreadable

THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
STAT = Path("/proc/stat")

# Beyond these a reading is not a board temperature but a broken file or a wrong unit.
COLDEST_C = -40.0
HOTTEST_C = 150.0


class Temperature:
    """The temperature of the chip, in degrees Celsius."""

    def __init__(self, path: Path = THERMAL) -> None:
        self.path = path

    def __call__(self) -> Sample:
        try:
            text = self.path.read_text()
        except OSError as error:
            raise Unreadable(f"this board has no thermal zone at {self.path}") from error
        try:
            degrees = int(text.strip()) / 1000
        except ValueError as error:
            raise Unreadable(f"the thermal zone answered {text.strip()!r}") from error
        if not COLDEST_C <= degrees <= HOTTEST_C:
            raise Unreadable(f"{degrees} °C is not a temperature this board can have")
        return Sample(round(degrees, 1))


class Cpu:
    """The share of the time since the last reading that the processor spent working."""

    def __init__(self, path: Path = STAT) -> None:
        self.path = path
        self._last: tuple[int, int] | None = None
        self._share = 0.0
        try:  # a baseline now, so the first scheduled reading already has one to compare to
            self._last = self._counters()
        except Unreadable:
            self._last = None

    def __call__(self) -> Sample:
        counters = self._counters()
        previous, self._last = self._last, counters
        if previous is None:
            raise Unreadable("there is nothing yet to compare this reading against")
        busy, total = counters[0] - previous[0], counters[1] - previous[1]
        if total > 0:
            # Asked again before the kernel's clock has ticked, the honest answer is the
            # one from the interval that did pass, not a nought and not a fault.
            self._share = max(0.0, min(100.0, 100 * busy / total))
        return Sample(round(self._share, 1))

    def _counters(self) -> tuple[int, int]:
        """Time spent working, and time spent at all, in the kernel's own units."""
        try:
            first = self.path.read_text().splitlines()[0]
        except (OSError, IndexError) as error:
            raise Unreadable(f"{self.path} does not say what the processor has done") from error
        fields = first.split()
        if fields[0] != "cpu" or len(fields) < 5:
            raise Unreadable(f"{self.path} starts with {first!r}, which is not the totals line")
        try:
            times = [int(field) for field in fields[1:9]]
        except ValueError as error:
            raise Unreadable(f"{self.path} has something other than numbers in it") from error
        idle = times[3] + times[4]  # idle and iowait: waiting, either way
        return sum(times) - idle, sum(times)

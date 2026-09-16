"""An analogue value through an ADS1115, the converter a light-dependent resistor needs.

The Pi has no analogue input, so an LDR on a bare GPIO reads nothing useful. Here it sits
in a divider on one ADS1115 channel. What comes out depends on the divider, so it is
labelled for what it is: `ratio` is the fraction of the reference voltage (0 to 1, a
relative figure) and `volts` is the measured voltage. Neither is lux.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sentry_satellite.sensors.i2c import Bus, BusError
from sentry_satellite.sensors.periodic import Sample, Unreadable

FULL_SCALE = 4.096
"""Gain setting 1 (PGA 001): ±4.096 V, enough for a divider on 3.3 V."""
OUTPUTS = {"ratio": "ratio", "volts": "V"}


def config_word(channel: int) -> int:
    """Single-shot, single-ended `channel`, ±4.096 V, 128 samples/s, comparator off."""
    return 0x8000 | ((4 + channel) << 12) | 0x0200 | 0x0100 | 0x0080 | 0x0003


class Ads1115:
    def __init__(
        self,
        bus: Bus,
        *,
        lock,
        channel: int,
        output: str = "ratio",
        reference_volts: float = 3.3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if channel not in range(4):
            raise ValueError("an ADS1115 has channels 0 to 3")
        if output not in OUTPUTS:
            raise ValueError(f"output is one of {', '.join(OUTPUTS)}")
        if reference_volts <= 0 or reference_volts > FULL_SCALE:
            raise ValueError(f"reference_volts must be above 0 and at most {FULL_SCALE}")
        self._bus = bus
        self._lock = lock
        self._channel = channel
        self._output = output
        self._reference = reference_volts
        self._clock = clock
        self._sleep = sleep

    @property
    def unit(self) -> str:
        return OUTPUTS[self._output]

    def __call__(self) -> Sample:
        with self._lock:
            try:
                volts = self._convert()
            except BusError as error:
                raise Unreadable(str(error)) from error
        if volts < -0.05 or volts > self._reference * 1.05:
            raise Unreadable(f"{volts:.3f} V is outside 0–{self._reference} V; check the divider")
        volts = min(max(volts, 0.0), self._reference)
        if self._output == "volts":
            return Sample(round(volts, 4))
        return Sample(round(volts / self._reference, 4))

    def _convert(self) -> float:
        self._bus.write(0x01, config_word(self._channel).to_bytes(2, "big"))
        deadline = self._clock() + 0.1
        self._sleep(0.009)
        while not int.from_bytes(self._bus.read(0x01, 2), "big") & 0x8000:
            if self._clock() > deadline:
                raise Unreadable("the conversion did not finish")
            self._sleep(0.002)
        raw = int.from_bytes(self._bus.read(0x00, 2), "big", signed=True)
        return raw * FULL_SCALE / 32768

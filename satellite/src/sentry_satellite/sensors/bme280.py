"""BME280 temperature, humidity and pressure over I2C, in forced mode.

A source is one quantity, so a BME280 used for all three is three sources on the same
bus and address. They share one `Bme280`, which takes a measurement at most once a
second and hands each source its part. Forced mode means the part sleeps between
readings, so it does not warm itself and bias its own temperature.

The compensation is the floating-point version from Bosch's datasheet (BST-BME280-DS002,
section 4.2.3), using the calibration the part carries in its own memory.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from sentry_satellite.sensors.i2c import Bus, BusError
from sentry_satellite.sensors.periodic import Sample, Unreadable

CHIP_ID = 0x60
QUANTITIES = {
    "temperature": ("climate.temperature", "°C"),
    "humidity": ("climate.humidity", "%"),
    "pressure": ("climate.pressure", "hPa"),
}


@dataclass(frozen=True)
class Calibration:
    t: tuple[int, int, int]
    p: tuple[int, int, int, int, int, int, int, int, int]
    h: tuple[int, int, int, int, int, int]

    @classmethod
    def parse(cls, first: bytes, second: bytes) -> Calibration:
        """`first` is 0x88–0xA1 (26 bytes), `second` is 0xE1–0xE7 (7 bytes)."""
        values = struct.unpack("<HhhHhhhhhhhh", first[:24])
        h1 = first[25]
        h2, h3 = struct.unpack("<hB", second[:3])
        e4, e5, e6 = second[3], second[4], second[5]
        h4 = _signed12((e4 << 4) | (e5 & 0x0F))
        h5 = _signed12((e6 << 4) | (e5 >> 4))
        h6 = struct.unpack("<b", second[6:7])[0]
        return cls(t=values[0:3], p=values[3:12], h=(h1, h2, h3, h4, h5, h6))


def _signed12(value: int) -> int:
    return value - 4096 if value & 0x800 else value


@dataclass(frozen=True)
class Measurement:
    temperature: float
    humidity: float | None
    pressure: float | None


def compensate(raw: bytes, calibration: Calibration) -> Measurement:
    """`raw` is 0xF7–0xFE: pressure, temperature and humidity, as the part left them."""
    adc_p = (raw[0] << 12) | (raw[1] << 4) | (raw[2] >> 4)
    adc_t = (raw[3] << 12) | (raw[4] << 4) | (raw[5] >> 4)
    adc_h = (raw[6] << 8) | raw[7]
    if adc_t == 0x80000:
        raise Unreadable("the temperature was not measured")
    t1, t2, t3 = calibration.t
    var1 = (adc_t / 16384.0 - t1 / 1024.0) * t2
    var2 = ((adc_t / 131072.0 - t1 / 8192.0) ** 2) * t3
    t_fine = var1 + var2
    temperature = t_fine / 5120.0

    pressure = None
    if adc_p != 0x80000:
        p1, p2, p3, p4, p5, p6, p7, p8, p9 = calibration.p
        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * p6 / 32768.0
        var2 = var2 + var1 * p5 * 2.0
        var2 = var2 / 4.0 + p4 * 65536.0
        var1 = (p3 * var1 * var1 / 524288.0 + p2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * p1
        if var1 != 0:
            value = 1048576.0 - adc_p
            value = (value - var2 / 4096.0) * 6250.0 / var1
            var1 = p9 * value * value / 2147483648.0
            var2 = value * p8 / 32768.0
            pressure = (value + (var1 + var2 + p7) / 16.0) / 100.0

    humidity = None
    if adc_h != 0x8000:
        h1, h2, h3, h4, h5, h6 = calibration.h
        value = t_fine - 76800.0
        value = (adc_h - (h4 * 64.0 + h5 / 16384.0 * value)) * (
            h2 / 65536.0 * (1.0 + h6 / 67108864.0 * value * (1.0 + h3 / 67108864.0 * value))
        )
        value = value * (1.0 - h1 * value / 524288.0)
        humidity = min(100.0, max(0.0, value))
    return Measurement(temperature=temperature, humidity=humidity, pressure=pressure)


class Bme280:
    """One physical part, shared by the sources that read it."""

    def __init__(
        self,
        bus: Bus,
        *,
        lock,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_age: float = 1.0,
    ) -> None:
        self._bus = bus
        self._lock = lock
        self._clock = clock
        self._sleep = sleep
        self._max_age = max_age
        self._calibration: Calibration | None = None
        self._last: tuple[float, Measurement] | None = None

    def measure(self) -> Measurement:
        with self._lock:
            if self._last is not None and self._clock() - self._last[0] < self._max_age:
                return self._last[1]
            try:
                measurement = self._measure()
            except BusError as error:
                self._calibration = None  # a part that was swapped has other constants
                raise Unreadable(str(error)) from error
            self._last = (self._clock(), measurement)
            return measurement

    def _measure(self) -> Measurement:
        if self._calibration is None:
            chip = self._bus.read(0xD0, 1)[0]
            if chip != CHIP_ID:
                raise Unreadable(f"chip id 0x{chip:02x} is not a BME280 (0x60)")
            self._calibration = Calibration.parse(self._bus.read(0x88, 26), self._bus.read(0xE1, 7))
        self._bus.write(0xF2, bytes([0x01]))  # humidity x1; takes effect with ctrl_meas
        self._bus.write(0xF4, bytes([0x25]))  # temperature x1, pressure x1, forced
        deadline = self._clock() + 0.1
        while self._bus.read(0xF3, 1)[0] & 0x08:
            if self._clock() > deadline:
                raise Unreadable("the measurement did not finish")
            self._sleep(0.005)
        return compensate(self._bus.read(0xF7, 8), self._calibration)

    def sampler(self, quantity: str) -> Callable[[], Sample]:
        if quantity not in QUANTITIES:
            raise ValueError(f"a BME280 measures {', '.join(QUANTITIES)}, not {quantity!r}")

        def sample() -> Sample:
            value = getattr(self.measure(), quantity)
            if value is None:
                raise Unreadable(f"the part did not measure {quantity}")
            return Sample(round(value, 2))

        return sample

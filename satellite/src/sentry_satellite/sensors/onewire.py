"""DS18B20 temperature over the kernel's 1-Wire driver (`dtoverlay=w1-gpio`).

The kernel does the bus work and exposes each probe as a file. Its first line ends in
`YES` when the CRC matched. The second line carries `t=` in thousandths of a degree.
A CRC mismatch, a missing file or a value the part cannot produce is `Unreadable`.
That includes 85 °C, the power-on value, which shows up when a probe loses power between
conversions.
"""

from __future__ import annotations

import re
from pathlib import Path

from sentry_satellite.sensors.periodic import Sample, Unreadable

DEVICE = re.compile(r"^28-[0-9a-f]{12}$")
DEVICES = Path("/sys/bus/w1/devices")
POWER_ON_MILLIDEGREES = 85000


def parse(text: str) -> float:
    lines = text.strip().splitlines()
    if len(lines) != 2:
        raise Unreadable("the probe answered with something other than two lines")
    if not lines[0].rstrip().endswith("YES"):
        raise Unreadable("CRC mismatch")
    _, found, value = lines[1].rpartition("t=")
    if not found:
        raise Unreadable("no temperature in the answer")
    try:
        millidegrees = int(value)
    except ValueError as error:
        raise Unreadable(f"unexpected temperature {value!r}") from error
    if millidegrees == POWER_ON_MILLIDEGREES:
        raise Unreadable("the probe reports its power-on value; check its supply")
    if not -55000 <= millidegrees <= 125000:
        raise Unreadable(f"{millidegrees / 1000} °C is outside what a DS18B20 measures")
    return millidegrees / 1000


class Ds18b20:
    def __init__(self, device: str, root: Path = DEVICES) -> None:
        if not DEVICE.fullmatch(device):
            raise ValueError(f"{device!r} is not a DS18B20 id (28-xxxxxxxxxxxx)")
        self.path = root / device / "w1_slave"

    def __call__(self) -> Sample:
        try:
            text = self.path.read_text(encoding="ascii", errors="replace")
        except FileNotFoundError as error:
            raise Unreadable(f"{self.path.parent.name} is not on the bus") from error
        except OSError as error:
            raise Unreadable(f"{self.path.parent.name}: {error.strerror or error}") from error
        return Sample(parse(text))

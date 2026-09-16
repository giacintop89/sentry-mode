"""Turning configured sources into drivers, and refusing the ones that do not exist yet.

Every kind in the configuration grammar is listed here, including the ones no driver has
been written for. A board configured for a camera it cannot drive should say so at
`validate`, not discover it at three in the morning by reporting nothing.
"""

from sentry_satellite.config import Config, Source
from sentry_satellite.sensors import Driver
from sentry_satellite.sensors.dummy import Motion

PENDING = {
    "gpio": "PR-06, once a sensor is wired to a real pin",
    "onewire": "PR-06",
    "bme280": "PR-06",
    "adc": "PR-06",
    "csi": "PR-08, once the camera profile is qualified",
    "uvc": "PR-08",
    "microphone": "PR-11",
    "ble": "PR-12",
}


class UnsupportedSource(RuntimeError):
    """The configuration asks for a driver this version of the agent does not have."""


def build_one(source: Source) -> Driver:
    if source.kind == "dummy":
        interval = float(source.options.get("interval_seconds", 1.0))
        return Motion(source.id, interval=interval)
    arriving = PENDING.get(source.kind, "a later increment")
    raise UnsupportedSource(f"{source.id}: a {source.kind} driver arrives with {arriving}")


def build(config: Config) -> list[Driver]:
    return [build_one(source) for source in config.sources]

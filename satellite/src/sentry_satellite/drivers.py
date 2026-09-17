"""Turning configured sources into drivers, and refusing the ones that do not exist yet.

Every kind in the configuration grammar is listed here, including the ones no driver has
been written for. A board configured for a camera it cannot drive should say so at
`validate`, not discover it at three in the morning by reporting nothing.

Hardware is reached through two factories, `open_line` and `open_i2c`. The tests replace
them; nothing else in the agent imports a GPIO or I2C module.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from sentry_satellite.audio.source import AlsaMicrophone
from sentry_satellite.camera.source import CsiCamera
from sentry_satellite.config import Config, Source
from sentry_satellite.presence.bluez import BluezScanner
from sentry_satellite.presence.service import BlePresence
from sentry_satellite.sensors import Driver
from sentry_satellite.sensors import gpio as gpio_lines
from sentry_satellite.sensors.adc import Ads1115
from sentry_satellite.sensors.bme280 import QUANTITIES, Bme280
from sentry_satellite.sensors.digital import DigitalInput
from sentry_satellite.sensors.dummy import Motion
from sentry_satellite.sensors.i2c import Bus, Device
from sentry_satellite.sensors.onewire import Ds18b20
from sentry_satellite.sensors.periodic import Periodic

SUPPORTED = ("gpio", "onewire", "bme280", "adc", "csi", "microphone", "ble", "dummy")
"""Kinds this version of the agent can drive. Remote configuration is limited to these."""

PENDING = {
    "uvc": "a later increment, once a USB camera is qualified on the Zero W",
}


class UnsupportedSource(RuntimeError):
    """The configuration asks for a driver this version of the agent does not have."""


@dataclass
class Hardware:
    """Where drivers get their lines and buses, shared across one set of drivers."""

    open_line: Callable[..., gpio_lines.Line] = gpio_lines.open_input
    open_i2c: Callable[[int, int], Bus] = Device
    encoder: str | None = None
    """The encoder program, when it is not the one on the PATH (the tests use a fake)."""
    recorder: str | None = None
    """The capture program, likewise."""
    consumer: str = "sentry-satellite"
    open_scanner: Callable[[str], BluezScanner] = BluezScanner
    """One Bluetooth scanner per adapter, shared by the sources that use it."""
    _buses: dict[tuple[int, int], Bus] = field(default_factory=dict)
    _parts: dict[tuple[int, int], Bme280] = field(default_factory=dict)
    _scanners: dict[str, BluezScanner] = field(default_factory=dict)

    def bus(self, number: int, address: int) -> Bus:
        key = (number, address)
        if key not in self._buses:
            self._buses[key] = self.open_i2c(number, address)
        return self._buses[key]

    def bme280(self, number: int, address: int) -> Bme280:
        key = (number, address)
        if key not in self._parts:
            bus = self.bus(number, address)
            self._parts[key] = Bme280(bus, lock=getattr(bus, "lock", None) or Lock())
        return self._parts[key]

    def scanner(self, adapter: str) -> BluezScanner:
        if adapter not in self._scanners:
            self._scanners[adapter] = self.open_scanner(adapter)
        return self._scanners[adapter]

    def close(self) -> None:
        for scanner in self._scanners.values():
            scanner.stop()
        self._scanners.clear()
        for bus in self._buses.values():
            bus.close()
        self._buses.clear()
        self._parts.clear()


def build_one(source: Source, hardware: Hardware | None = None) -> Driver:
    hardware = hardware or Hardware()
    options = source.options
    if source.kind == "csi":
        return CsiCamera(source.id, options, program=hardware.encoder)
    if source.kind == "microphone":
        return AlsaMicrophone(source.id, options, program=hardware.recorder)
    if source.kind == "ble":
        return BlePresence(source.id, options, scanner=hardware.scanner(options["adapter"]))
    if source.kind == "dummy":
        return Motion(source.id, interval=float(options.get("interval_seconds", 1.0)))
    if source.kind == "gpio":
        chip, line, bias = options["chip"], options["line"], options["bias"]
        return DigitalInput(
            source.id,
            lambda: hardware.open_line(
                chip, line, consumer=f"{hardware.consumer}:{source.id}", bias=bias
            ),
            event_kind=options["event_kind"],
            active_high=options["active_high"],
            debounce_ms=options["debounce_ms"],
            settle_seconds=options["settle_seconds"],
        )
    if source.kind == "onewire":
        return Periodic(
            source.id,
            Ds18b20(options["device"]),
            event_kind=options["event_kind"],
            unit="°C",
            interval_seconds=options["interval_seconds"],
        )
    if source.kind == "bme280":
        part = hardware.bme280(options["bus"], options["address"])
        kind, unit = QUANTITIES[options["measure"]]
        return Periodic(
            source.id,
            part.sampler(options["measure"]),
            event_kind=kind,
            unit=unit,
            interval_seconds=options["interval_seconds"],
        )
    if source.kind == "adc":
        bus = hardware.bus(options["bus"], options["address"])
        converter = Ads1115(
            bus,
            lock=getattr(bus, "lock", None) or Lock(),
            channel=options["channel"],
            output=options["output"],
            reference_volts=options["reference_volts"],
        )
        return Periodic(
            source.id,
            converter,
            event_kind=options["event_kind"],
            unit=converter.unit,
            interval_seconds=options["interval_seconds"],
        )
    arriving = PENDING.get(source.kind, "a later increment")
    raise UnsupportedSource(f"{source.id}: a {source.kind} driver arrives with {arriving}")


def build(config: Config, hardware: Hardware | None = None) -> list[Driver]:
    """Drivers for every enabled source. Nothing is opened until a driver starts reading."""
    hardware = hardware or Hardware()
    return [build_one(source, hardware) for source in config.sources if source.enabled]


class Builder:
    """Builds one set of drivers at a time and releases the buses of the set before it.

    `check` builds with hardware that is thrown away: nothing is opened until a driver
    reads, so it proves the configuration can be driven without touching a pin.
    """

    kinds = SUPPORTED

    def __init__(self, hardware: Callable[[], Hardware] = Hardware) -> None:
        self._hardware_factory = hardware
        self._hardware: Hardware | None = None

    def check(self, config: Config) -> None:
        build(config, self._hardware_factory())

    def __call__(self, config: Config) -> list[Driver]:
        self.release()
        self._hardware = self._hardware_factory()
        return build(config, self._hardware)

    def release(self) -> None:
        if self._hardware is not None:
            self._hardware.close()
            self._hardware = None

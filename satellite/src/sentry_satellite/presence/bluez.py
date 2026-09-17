"""Bluetooth LE advertisements, heard through BlueZ, for the devices someone asked about.

One scanner per adapter, shared by every `ble` source on the board. It asks BlueZ for LE
discovery with duplicates reported, and turns each fresh advertisement from a device a
source is interested in into a sighting. What BlueZ already had cached when the scanner
started is never a sighting: a device object that is still there says only that the
device was heard at some time, not now.

A device is matched by its address or by the iBeacon it broadcasts. Addresses stay in this
module: nothing outside it, not the logs and not the hub, learns the address of any device,
wanted or not.

The scanner also says whether it is actually scanning. A source may only decide that a
device is absent while it is: an adapter that is off, blocked, reset or taken away, a
bluetoothd that restarted, or a discovery that stopped all make the answer unknown.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sentry_satellite.presence.dbus import (
    BUS_NAME,
    Connection,
    DBusError,
    Message,
    Variant,
    plain,
)

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
OBJECTS = "org.freedesktop.DBus.ObjectManager"
APPLE = 0x004C
IBEACON = b"\x02\x15"
QUEUE = 64
"""What one source may fall behind by before its oldest news is dropped."""
ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


@dataclass(frozen=True)
class Matcher:
    """The one device a source is about."""

    address: str | None = None
    ibeacon_uuid: str | None = None
    major: int | None = None
    minor: int | None = None

    def __post_init__(self) -> None:
        if (self.address is None) == (self.ibeacon_uuid is None):
            raise ValueError("a device is matched by its address or by its iBeacon, not both")

    def keys(self) -> list[bytes]:
        """Bytes that a signal about this device necessarily contains."""
        if self.address is not None:
            return [b"dev_" + self.address.replace(":", "_").encode()]
        assert self.ibeacon_uuid is not None
        return [uuid.UUID(self.ibeacon_uuid).bytes]

    def matches(self, device: dict) -> bool:
        if self.address is not None:
            return device.get("Address", "").upper() == self.address
        beacon = ibeacon(device.get("ManufacturerData"))
        if beacon is None or self.ibeacon_uuid is None:
            return False
        found, major, minor = beacon
        return (
            found == uuid.UUID(self.ibeacon_uuid)
            and self.major in (None, major)
            and self.minor in (None, minor)
        )


def ibeacon(manufacturer: Any) -> tuple[uuid.UUID, int, int] | None:
    data = (manufacturer or {}).get(APPLE)
    if not isinstance(data, (bytes, bytearray)) or len(data) < 23 or data[:2] != IBEACON:
        return None
    major, minor = struct.unpack(">HH", data[18:22])
    return uuid.UUID(bytes=bytes(data[2:18])), major, minor


@dataclass(frozen=True)
class Sighting:
    at: float
    rssi: int | None


@dataclass(frozen=True)
class Coverage:
    """The scanner started or stopped scanning."""

    at: float
    scanning: bool
    reason: str | None = None


News = Sighting | Coverage


class Watch:
    """One source's view of the scanner: its sightings and the scanner's state, in order."""

    def __init__(self, matcher: Matcher, limit: int = QUEUE) -> None:
        self.matcher = matcher
        self.limit = limit
        self.dropped = 0
        self._news: deque[News] = deque()
        self._ready = threading.Condition()

    def put(self, item: News) -> None:
        with self._ready:
            if len(self._news) >= self.limit:
                # Older sightings are worth least; a change of coverage is never dropped.
                for index, old in enumerate(self._news):
                    if isinstance(old, Sighting):
                        del self._news[index]
                        break
                else:
                    self._news.popleft()
                self.dropped += 1
            self._news.append(item)
            self._ready.notify()

    def get(self, timeout: float) -> News | None:
        with self._ready:
            if not self._news:
                self._ready.wait(timeout)
            return self._news.popleft() if self._news else None


@dataclass
class _Device:
    properties: dict = field(default_factory=dict)
    watches: tuple[Watch, ...] = ()


class BluezScanner:
    def __init__(
        self,
        adapter: str = "hci0",
        *,
        connect: Callable[[], Any] = Connection,
        clock: Callable[[], float] = time.monotonic,
        retry_seconds: float = 5.0,
        rediscover_seconds: float = 10.0,
    ) -> None:
        self.adapter = adapter
        self.path = f"/org/bluez/{adapter}"
        self._connect = connect
        self._clock = clock
        self.retry_seconds = retry_seconds
        self.rediscover_seconds = rediscover_seconds
        self._lock = threading.Lock()
        self._watches: list[Watch] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection: Any = None
        self._devices: dict[str, _Device] = {}
        self._keys: tuple[bytes, ...] = ()
        self._matched: set[bytes] = set()
        """The object paths of devices somebody wants, which a bare RSSI update names."""
        self.scanning = False
        self.error: str | None = None
        self.sessions = 0
        self.sightings = 0
        self.ignored = 0

    # -- sources -------------------------------------------------------------------

    def subscribe(self, matcher: Matcher) -> Watch:
        watch = Watch(matcher)
        with self._lock:
            self._watches.append(watch)
            self._keys = tuple(key for w in self._watches for key in w.matcher.keys())
            self._rematch()
            start = self._thread is None or not self._thread.is_alive()
            if not start:
                watch.put(Coverage(self._clock(), self.scanning, self.error))
        if start:
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop,), name=f"ble:{self.adapter}", daemon=True
            )
            self._thread.start()
        return watch

    def unsubscribe(self, watch: Watch) -> None:
        with self._lock:
            if watch in self._watches:
                self._watches.remove(watch)
            self._keys = tuple(key for w in self._watches for key in w.matcher.keys())
            self._rematch()
            last = not self._watches
        if last:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            connection.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def status(self) -> dict:
        with self._lock:
            return {
                "adapter": self.adapter,
                "scanning": self.scanning,
                "error": self.error,
                "sessions": self.sessions,
                "sightings": self.sightings,
                "ignored_signals": self.ignored,
                "watching": len(self._watches),
            }

    # -- the session -----------------------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._session(stop)
            except DBusError as error:
                if not stop.is_set():
                    self._coverage(False, str(error))
                    log.warning("Bluetooth scanning on %s stopped: %s", self.adapter, error)
            except Exception as error:  # noqa: BLE001 - a scanner bug must not end presence
                log.exception("Bluetooth scanning on %s failed", self.adapter)
                self._coverage(False, f"the scanner failed: {error}")
            finally:
                connection, self._connection = self._connection, None
                if connection is not None:
                    connection.close()
            stop.wait(self.retry_seconds)
        self._coverage(False, "stopped")

    def _session(self, stop: threading.Event) -> None:
        connection = self._connect()
        self._connection = connection
        if stop.is_set():
            return
        with self._lock:
            self.sessions += 1
            self._devices = {}
            self._matched = set()
        connection.add_match(
            f"type='signal',sender='{BUS_NAME}',member='NameOwnerChanged',arg0='{BLUEZ}'"
        )
        connection.add_match(f"type='signal',sender='{BLUEZ}',interface='{OBJECTS}'")
        connection.add_match(
            f"type='signal',sender='{BLUEZ}',interface='{PROPERTIES}',"
            f"member='PropertiesChanged',path_namespace='{self.path}'"
        )
        objects = connection.call(BLUEZ, "/", OBJECTS, "GetManagedObjects")[0]
        adapter = objects.get(self.path, {}).get(ADAPTER)
        if adapter is None:
            raise DBusError(f"there is no Bluetooth adapter {self.adapter}")
        # What BlueZ remembers is kept to match later advertisements against, never counted.
        with self._lock:
            for path, interfaces in objects.items():
                if path.startswith(self.path + "/") and DEVICE in interfaces:
                    self._devices[path] = _Device(dict(interfaces[DEVICE]))
            self._rematch()
        self._power(connection, adapter)
        self._discover(connection)
        next_try = self._clock() + self.rediscover_seconds
        while not stop.is_set():
            message = connection.next_signal(1.0, self._wanted)
            if message is not None:
                self._handle(message)
            if not self.scanning and self._clock() >= next_try:
                next_try = self._clock() + self.rediscover_seconds
                self._discover(connection)

    def _power(self, connection: Any, adapter: dict) -> None:
        if adapter.get("Powered"):
            return
        if adapter.get("PowerState") == "off-blocked":
            raise DBusError(f"{self.adapter} is blocked (rfkill unblock bluetooth)")
        try:
            connection.call(
                BLUEZ, self.path, PROPERTIES, "Set", "ssv", (ADAPTER, "Powered", Variant("b", True))
            )
        except DBusError as error:
            raise DBusError(f"{self.adapter} could not be switched on: {error}") from error

    def _discover(self, connection: Any) -> None:
        connection.call(
            BLUEZ,
            self.path,
            ADAPTER,
            "SetDiscoveryFilter",
            "a{sv}",
            ({"Transport": Variant("s", "le"), "DuplicateData": Variant("b", True)},),
        )
        try:
            connection.call(BLUEZ, self.path, ADAPTER, "StartDiscovery")
        except DBusError as error:
            if error.name != "org.bluez.Error.InProgress":
                self._coverage(False, f"discovery did not start: {error}")
                return
        discovering = connection.call(
            BLUEZ, self.path, PROPERTIES, "Get", "ss", (ADAPTER, "Discovering")
        )
        self._coverage(bool(discovering[0]), None if discovering[0] else "discovery did not start")

    # -- signals ---------------------------------------------------------------------

    def _wanted(self, raw: bytes) -> bool:
        if any(key in raw for key in self._keys):
            return True
        if self._matched and any(path in raw for path in self._matched):
            return True
        if b"InterfacesRemoved" in raw or b"NameOwnerChanged" in raw:
            return True
        if ADAPTER.encode() in raw:
            return True
        self.ignored += 1
        return False

    def _handle(self, message: Message) -> None:
        member, body = message.member, message.body
        if member == "NameOwnerChanged":
            if not body[2]:
                raise DBusError("bluetoothd went away")
            return
        if member == "InterfacesAdded":
            path, interfaces = body[0], plain(body[1])
            if DEVICE in interfaces:
                self._update(path, interfaces[DEVICE], fresh="RSSI" in interfaces[DEVICE])
            return
        if member == "InterfacesRemoved":
            path, interfaces = body
            if path == self.path and ADAPTER in interfaces:
                raise DBusError(f"{self.adapter} was removed")
            if DEVICE in interfaces:
                with self._lock:
                    self._devices.pop(path, None)
                    self._matched.discard(path.encode())
            return
        if member != "PropertiesChanged":
            return
        interface, changed = body[0], plain(body[1])
        if message.path == self.path and interface == ADAPTER:
            if changed.get("Powered") is False:
                raise DBusError(f"{self.adapter} was switched off")
            if "Discovering" in changed:
                scanning = bool(changed["Discovering"])
                self._coverage(scanning, None if scanning else "discovery stopped")
        elif interface == DEVICE and message.path is not None:
            self._update(message.path, changed, fresh="RSSI" in changed)

    def _update(self, path: str, properties: dict, *, fresh: bool) -> None:
        now = self._clock()
        with self._lock:
            device = self._devices.setdefault(path, _Device())
            before = device.properties
            device.properties = {**before, **properties}
            if "Address" in properties or "ManufacturerData" in properties:
                device.watches = self._interested(device.properties)
                if device.watches:
                    self._matched.add(path.encode())
                else:
                    self._matched.discard(path.encode())
            watches = device.watches
            if not (fresh and watches and self.scanning):
                return
            self.sightings += 1
        rssi = properties.get("RSSI")
        for watch in watches:
            watch.put(Sighting(now, rssi if isinstance(rssi, int) else None))

    def _rematch(self) -> None:
        """Match every known device again. Called with the lock held."""
        self._matched = set()
        for path, device in self._devices.items():
            device.watches = self._interested(device.properties)
            if device.watches:
                self._matched.add(path.encode())

    def _interested(self, properties: dict) -> tuple[Watch, ...]:
        if "Address" not in properties and "ManufacturerData" not in properties:
            return ()
        return tuple(w for w in self._watches if w.matcher.matches(properties))

    def _coverage(self, scanning: bool, reason: str | None) -> None:
        now = self._clock()
        with self._lock:
            changed = scanning != self.scanning or reason != self.error
            self.scanning, self.error = scanning, reason
            watches = list(self._watches)
        if changed:
            for watch in watches:
                watch.put(Coverage(now, scanning, reason))


def valid_address(value: str) -> bool:
    return bool(ADDRESS.fullmatch(value))


__all__ = ["BluezScanner", "Coverage", "Matcher", "Sighting", "Watch", "ibeacon"]

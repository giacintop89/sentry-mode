"""Bluetooth presence: the bus client, the scanner, and what makes a device present.

The D-Bus client is tested against a socket that speaks the protocol back, so the
marshalling is exercised for real; the scanner against a bus that says what BlueZ says.
Nothing here needs a Bluetooth adapter.
"""

import socket
import struct
import threading
import time
import uuid

import pytest

from sentry_satellite import config as configuration
from sentry_satellite import drivers
from sentry_satellite.presence import dbus
from sentry_satellite.presence.bluez import (
    ADAPTER,
    BLUEZ,
    DEVICE,
    OBJECTS,
    PROPERTIES,
    BluezScanner,
    Coverage,
    Matcher,
    Sighting,
    ibeacon,
)
from sentry_satellite.presence.service import UNKNOWN, BlePresence, Presence

TAG = "AA:BB:CC:DD:EE:FF"
TAG_PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
OTHER_PATH = "/org/bluez/hci0/dev_11_22_33_44_55_66"
BEACON = "f7826da6-4fa2-4e98-8024-bc5b71e0893e"


def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- the bus client ----------------------------------------------------------------------


class FakeBus:
    """A socket that authenticates, answers Hello, and says what a test tells it to."""

    def __init__(self, path):
        self.path = str(path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.calls = []
        self.answers = {}
        self.client = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.client, _ = self.server.accept()
        greeting = b""
        welcomed = False
        while b"BEGIN\r\n" not in greeting:
            chunk = self.client.recv(256)
            if not chunk:
                return
            greeting += chunk
            if not welcomed and greeting.startswith(b"\0AUTH EXTERNAL") and b"\r\n" in greeting:
                welcomed = True
                self.client.sendall(b"OK 1234\r\n")
        buffer = greeting.split(b"BEGIN\r\n", 1)[1]  # the first call can ride along with BEGIN
        while True:
            while len(buffer) >= 16 and len(buffer) >= dbus.message_size(buffer[:16]):
                size = dbus.message_size(buffer[:16])
                message, buffer = dbus.decode(buffer[:size]), buffer[size:]
                self._answer(message)
            try:
                chunk = self.client.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk

    def _answer(self, message):
        self.calls.append((message.member, message.body))
        answer = self.answers.get(message.member, ("", ()))
        if isinstance(answer, Exception):
            reply = dbus.Message(
                dbus.ERROR,
                0,
                {"reply_serial": message.serial, "error_name": str(answer), "signature": "s"},
                ("no",),
            )
        else:
            signature, body = answer
            reply = dbus.Message(
                dbus.METHOD_RETURN,
                0,
                {"reply_serial": message.serial, "signature": signature},
                body,
            )
        self.send(reply)

    def send(self, message):
        self.client.sendall(dbus.encode(message))

    def signal(self, path, interface, member, signature, body):
        self.send(
            dbus.Message(
                dbus.SIGNAL,
                0,
                {
                    "path": path,
                    "interface": interface,
                    "member": member,
                    "sender": ":1.1",
                    "signature": signature,
                },
                body,
            )
        )

    def close(self):
        self.server.close()
        if self.client is not None:
            self.client.close()


@pytest.fixture
def bus(tmp_path):
    made = FakeBus(tmp_path / "bus")
    made.answers["Hello"] = ("s", (":1.42",))
    yield made
    made.close()


def test_the_client_authenticates_and_says_hello(bus):
    connection = dbus.Connection(bus.path)
    assert connection.unique_name == ":1.42"
    assert bus.calls[0][0] == "Hello"
    connection.close()


def test_a_call_carries_every_type_the_agent_uses(bus):
    objects = {
        "/org/bluez/hci0": {ADAPTER: {"Powered": dbus.Variant("b", True)}},
        TAG_PATH: {
            DEVICE: {
                "Address": dbus.Variant("s", TAG),
                "RSSI": dbus.Variant("n", -61),
                "ManufacturerData": dbus.Variant(
                    "a{qv}", {0x4C: dbus.Variant("ay", b"\x02\x15" + bytes(21))}
                ),
            }
        },
    }
    bus.answers["GetManagedObjects"] = ("a{oa{sa{sv}}}", (objects,))
    connection = dbus.Connection(bus.path)
    got = connection.call(BLUEZ, "/", OBJECTS, "GetManagedObjects")[0]
    assert got["/org/bluez/hci0"][ADAPTER]["Powered"] is True
    device = got[TAG_PATH][DEVICE]
    assert (device["Address"], device["RSSI"]) == (TAG, -61)
    assert device["ManufacturerData"][0x4C][:2] == b"\x02\x15"
    connection.send(BLUEZ, "/org/bluez/hci0", ADAPTER, "SetDiscoveryFilter", "a{sv}",
                    ({"Transport": dbus.Variant("s", "le")},), reply=False)  # fmt: skip
    assert until(lambda: bus.calls[-1][0] == "SetDiscoveryFilter")
    assert bus.calls[-1][1] == ({"Transport": dbus.Variant("s", "le")},)
    connection.close()


def test_an_error_reply_says_which_error_it_was(bus):
    bus.answers["StartDiscovery"] = DBusName = Exception("org.bluez.Error.InProgress")
    connection = dbus.Connection(bus.path)
    with pytest.raises(dbus.DBusError) as raised:
        connection.call(BLUEZ, "/org/bluez/hci0", ADAPTER, "StartDiscovery")
    assert raised.value.name == "org.bluez.Error.InProgress"
    assert str(DBusName) in raised.value.name
    connection.close()


def test_signals_that_arrive_during_a_call_are_not_lost(bus):
    connection = dbus.Connection(bus.path)
    bus.signal(TAG_PATH, PROPERTIES, "PropertiesChanged", "sa{sv}as",
               (DEVICE, {"RSSI": dbus.Variant("n", -70)}, []))  # fmt: skip
    bus.answers["Get"] = ("v", (dbus.Variant("b", True),))
    assert connection.call(BLUEZ, "/org/bluez/hci0", PROPERTIES, "Get", "ss", (ADAPTER, "Powered"))
    signal = connection.next_signal(2)
    assert signal is not None and signal.member == "PropertiesChanged"
    assert signal.body[1]["RSSI"].value == -70
    connection.close()


def test_a_signal_nobody_wants_is_never_decoded(bus):
    connection = dbus.Connection(bus.path)
    bus.signal(OTHER_PATH, PROPERTIES, "PropertiesChanged", "sa{sv}as",
               (DEVICE, {"RSSI": dbus.Variant("n", -70)}, []))  # fmt: skip
    bus.signal(TAG_PATH, PROPERTIES, "PropertiesChanged", "sa{sv}as",
               (DEVICE, {"RSSI": dbus.Variant("n", -50)}, []))  # fmt: skip
    kept = connection.next_signal(2, lambda raw: b"dev_AA_BB_CC_DD_EE_FF" in raw)
    assert kept is not None and kept.path == TAG_PATH
    assert connection.skipped == 1
    connection.close()


def test_a_message_the_client_does_not_speak_is_refused():
    with pytest.raises(dbus.DBusError, match="little-endian"):
        dbus.message_size(b"B" + bytes(15))
    with pytest.raises(dbus.DBusError, match="not speak"):
        dbus.split_signature("aQ")
    assert dbus.split_signature("a{sv}oay") == ["a{sv}", "o", "ay"]


# -- matching ----------------------------------------------------------------------------


def beacon_data(uuid_text=BEACON, major=1, minor=2):
    return {0x4C: b"\x02\x15" + uuid.UUID(uuid_text).bytes + struct.pack(">HHb", major, minor, -59)}


def test_a_device_is_matched_by_address_or_by_its_beacon():
    assert Matcher(address=TAG).matches({"Address": TAG})
    assert not Matcher(address=TAG).matches({"Address": "11:22:33:44:55:66"})
    beacon = Matcher(ibeacon_uuid=BEACON, major=1, minor=2)
    assert beacon.matches({"ManufacturerData": beacon_data()})
    assert not beacon.matches({"ManufacturerData": beacon_data(minor=3)})
    assert Matcher(ibeacon_uuid=BEACON).matches({"ManufacturerData": beacon_data(minor=9)})
    assert not beacon.matches({"ManufacturerData": {0x4C: b"\x10\x15" + bytes(21)}})
    assert not beacon.matches({"Address": TAG})
    assert ibeacon({0x4C: b"\x02\x15"}) is None
    with pytest.raises(ValueError, match="not both"):
        Matcher(address=TAG, ibeacon_uuid=BEACON)


# -- the scanner -------------------------------------------------------------------------


class FakeConnection:
    """A bus that answers the calls the scanner makes and hands it prepared signals."""

    def __init__(self, objects=None, *, powered=True, power_state="on", refuse=None):
        self.objects = objects if objects is not None else {}
        self.adapter = {"Powered": powered, "PowerState": power_state, "Discovering": False}
        if self.objects is not None and "/org/bluez/hci0" not in self.objects:
            self.objects["/org/bluez/hci0"] = {ADAPTER: self.adapter}
        self.refuse = refuse or {}
        self.calls = []
        self.matches = []
        self.signals = []
        self.closed = threading.Event()
        self._ready = threading.Condition()

    def add_match(self, rule):
        self.matches.append(rule)

    def call(self, destination, path, interface, member, signature="", body=()):
        self.calls.append((member, path, body))
        if member in self.refuse:
            raise dbus.DBusError(f"{member} refused", self.refuse[member])
        if member == "GetManagedObjects":
            return (self.objects,)
        if member == "Get":
            return (self.adapter.get(body[1]),)
        if member == "Set":
            self.adapter[body[1]] = body[2].value
            return ()
        if member == "StartDiscovery":
            self.adapter["Discovering"] = True
            return ()
        return ()

    def push(self, path, interface, member, body):
        with self._ready:
            self.signals.append(dbus.Message(dbus.SIGNAL, 0, {
                "path": path, "interface": interface, "member": member, "sender": ":1.1",
            }, body, raw=b"raw " + repr(body).encode()))  # fmt: skip
            self._ready.notify()

    def next_signal(self, timeout, keep=None):
        with self._ready:
            if not self.signals:
                self._ready.wait(timeout)
            return self.signals.pop(0) if self.signals else None

    def close(self):
        self.closed.set()
        with self._ready:
            self._ready.notify_all()


def scanner_with(connection, **options):
    made = BluezScanner("hci0", connect=lambda: connection, retry_seconds=0.05, **options)
    return made


def changed(properties):
    return (DEVICE, properties, [])


def test_a_device_bluez_already_knew_is_not_a_sighting():
    bus = FakeConnection({TAG_PATH: {DEVICE: {"Address": TAG, "RSSI": -60}}})
    scanner = scanner_with(bus)
    watch = scanner.subscribe(Matcher(address=TAG))
    try:
        assert until(lambda: scanner.status()["scanning"])
        assert isinstance(watch.get(1), Coverage)
        assert watch.get(0.2) is None
        bus.push(TAG_PATH, PROPERTIES, "PropertiesChanged", changed({"RSSI": -61}))
        news = watch.get(2)
        assert isinstance(news, Sighting) and news.rssi == -61
        assert scanner.status()["sightings"] == 1
    finally:
        scanner.stop()


def test_a_new_beacon_is_a_sighting_and_another_device_is_not():
    bus = FakeConnection()
    scanner = scanner_with(bus)
    watch = scanner.subscribe(Matcher(ibeacon_uuid=BEACON))
    try:
        assert until(lambda: scanner.status()["scanning"])
        bus.push("/", OBJECTS, "InterfacesAdded",
                 (OTHER_PATH, {DEVICE: {"Address": "11:22:33:44:55:66", "RSSI": -55}}))  # fmt: skip
        seen = {DEVICE: {"ManufacturerData": beacon_data(), "RSSI": -55}}
        bus.push("/", OBJECTS, "InterfacesAdded", (TAG_PATH, seen))
        news = watch.get(2)
        while isinstance(news, Coverage):
            news = watch.get(2)
        assert isinstance(news, Sighting) and news.rssi == -55
        assert scanner.status()["sightings"] == 1
        # A later advertisement of the same beacon carries only its strength.
        bus.push(TAG_PATH, PROPERTIES, "PropertiesChanged", changed({"RSSI": -58}))
        assert isinstance(watch.get(2), Sighting)
    finally:
        scanner.stop()


def test_a_discovery_that_stops_is_told_to_the_sources_and_started_again():
    bus = FakeConnection()
    scanner = scanner_with(bus, rediscover_seconds=0.1)
    watch = scanner.subscribe(Matcher(address=TAG))
    try:
        assert until(lambda: scanner.status()["scanning"])
        bus.adapter["Discovering"] = False
        bus.push("/org/bluez/hci0", PROPERTIES, "PropertiesChanged",
                 (ADAPTER, {"Discovering": False}, []))  # fmt: skip
        assert until(lambda: not scanner.status()["scanning"])
        assert any(isinstance(n, Coverage) and not n.scanning for n in _drain(watch))
        assert until(lambda: scanner.status()["scanning"], timeout=3)
        assert sum(call[0] == "StartDiscovery" for call in bus.calls) >= 2
    finally:
        scanner.stop()


def test_an_adapter_that_is_blocked_or_missing_is_reported_and_tried_again():
    bus = FakeConnection(powered=False, power_state="off-blocked")
    scanner = scanner_with(bus)
    watch = scanner.subscribe(Matcher(address=TAG))
    try:
        assert until(lambda: "blocked" in (scanner.status()["error"] or ""))
        assert not scanner.status()["scanning"]
        assert any(isinstance(n, Coverage) and not n.scanning for n in _drain(watch))
    finally:
        scanner.stop()
    missing = FakeConnection({})
    missing.objects.clear()
    gone = scanner_with(missing)
    gone.subscribe(Matcher(address=TAG))
    try:
        assert until(lambda: "no Bluetooth adapter" in (gone.status()["error"] or ""))
        assert until(lambda: gone.status()["sessions"] >= 2, timeout=3)
    finally:
        gone.stop()


def test_bluetoothd_going_away_starts_a_new_session():
    bus = FakeConnection()
    scanner = scanner_with(bus)
    scanner.subscribe(Matcher(address=TAG))
    try:
        assert until(lambda: scanner.status()["scanning"])
        bus.push(None, "org.freedesktop.DBus", "NameOwnerChanged", (BLUEZ, ":1.1", ""))
        assert until(lambda: scanner.status()["sessions"] >= 2, timeout=3)
        assert bus.closed.is_set()
    finally:
        scanner.stop()


def test_the_scanner_stops_when_the_last_source_lets_go():
    bus = FakeConnection()
    scanner = scanner_with(bus)
    first = scanner.subscribe(Matcher(address=TAG))
    second = scanner.subscribe(Matcher(ibeacon_uuid=BEACON))
    assert until(lambda: scanner.status()["watching"] == 2)
    scanner.unsubscribe(first)
    assert scanner.status()["scanning"]
    scanner.unsubscribe(second)
    assert until(lambda: not scanner.status()["scanning"])
    assert bus.closed.is_set()


def _drain(watch):
    news = []
    while True:
        item = watch.get(0.05)
        if item is None:
            return news
        news.append(item)


# -- what makes a device present ---------------------------------------------------------


def test_three_sightings_over_a_few_seconds_make_it_present():
    presence = Presence(enter_sightings=3, enter_window_seconds=10, absent_after_seconds=60)
    assert presence.coverage(True, 0) is None
    assert presence.seen(1.0, -60) is None
    assert presence.seen(1.1, -60) is None  # the same advertisement burst, not a second hit
    assert presence.seen(2.5, -60) is None
    assert presence.seen(4.0, -60) == "present"
    assert presence.state == "present" and presence.sightings == 4


def test_sightings_spread_beyond_the_window_never_add_up():
    presence = Presence(enter_sightings=3, enter_window_seconds=10, absent_after_seconds=60)
    presence.coverage(True, 0)
    for at in (1.0, 8.0, 20.0, 40.0):
        assert presence.seen(at, -60) is None
    assert presence.state == UNKNOWN


def test_a_signal_too_weak_for_this_room_is_not_a_sighting():
    presence = Presence(enter_sightings=1, rssi_min=-70)
    presence.coverage(True, 0)
    assert presence.seen(1.0, -80) is None
    assert presence.weak == 1
    assert presence.seen(2.0, -65) == "present"


def test_absence_needs_a_scanner_that_was_scanning_the_whole_time():
    presence = Presence(enter_sightings=1, absent_after_seconds=100)
    presence.coverage(True, 0)
    presence.seen(1.0, -60)
    assert presence.tick(50) is None
    assert presence.tick(101) == "absent"
    assert presence.tick(300) is None  # said once
    # A scanner that stops makes it unknown, and the wait starts again when it is back.
    assert presence.coverage(False, 310) == UNKNOWN
    assert presence.tick(500) is None
    assert presence.coverage(True, 600) is None
    assert presence.tick(650) is None
    assert presence.tick(701) == "absent"


def test_nothing_is_concluded_while_the_scanner_is_down():
    presence = Presence(enter_sightings=1)
    assert presence.seen(1.0, -50) is None
    assert presence.state == UNKNOWN


# -- the source ---------------------------------------------------------------------------


class FakeScanner:
    def __init__(self):
        self.watches = []
        self.released = []

    def subscribe(self, matcher):
        from sentry_satellite.presence.bluez import Watch

        watch = Watch(matcher)
        self.watches.append(watch)
        return watch

    def unsubscribe(self, watch):
        self.released.append(watch)

    def stop(self):
        self.stopped = True

    def status(self):
        return {"scanning": True}


def readings_of(driver, count, feed):
    got = []
    stream = driver.read()
    threading.Timer(0.05, feed).start()  # the source subscribes when it is first read
    for reading in stream:
        got.append(reading)
        if len(got) == count:
            break
    return got


def test_a_source_reports_where_the_device_stands_and_never_who_it_is():
    scanner = FakeScanner()
    driver = BlePresence(
        "tag-1",
        {"address": TAG, "enter_sightings": 2, "absent_after_seconds": 10},
        scanner=scanner,
        clock=lambda: 0.0,
        tick_seconds=0.01,
    )

    def feed():
        watch = scanner.watches[0]
        watch.put(Coverage(0.0, True))
        watch.put(Sighting(1.0, -55))
        watch.put(Sighting(3.0, -55))

    first = readings_of(driver, 1, feed)[0]
    assert (first.kind, first.value, first.quality) == ("presence.state", "present", "valid")
    assert first.initial is True  # from unknown: where things stand, not an arrival
    assert TAG not in str(first)
    driver.stop()
    assert driver.presence()["state"] == "present" and driver.presence()["rssi"] == -55


def test_a_scanner_that_stops_makes_the_source_unknown():
    scanner = FakeScanner()
    driver = BlePresence(
        "tag-1",
        {"address": TAG, "enter_sightings": 1},
        scanner=scanner,
        clock=lambda: 2.0,
        tick_seconds=0.01,
    )

    def feed():
        watch = scanner.watches[0]
        watch.put(Coverage(0.0, True))
        watch.put(Sighting(1.0, -55))
        watch.put(Coverage(2.0, False, "discovery stopped"))

    got = readings_of(driver, 2, feed)
    driver.stop()
    assert [(r.value, r.quality) for r in got] == [("present", "valid"), (None, "unknown")]
    assert got[1].initial is False
    assert driver.presence()["error"] == "discovery stopped"
    assert scanner.released


# -- configuration -------------------------------------------------------------------------


def source(**options):
    return {"id": "tag-1", "kind": "ble", **options}


def load(sources, profile="sensor-presence"):
    return configuration.parse_sources(
        sources, node_id="zero-entrance", profile=profile, where="sources"
    )


def named(thing) -> str:
    if isinstance(thing, dict):
        return str(thing.get("id", ""))
    return str(getattr(thing, "source_id", None) or getattr(thing, "id", thing))


def wired(things):
    """What the configuration asked for, without the two readings every board takes."""
    return [thing for thing in things if not named(thing).startswith("board-")]


def test_a_ble_source_needs_exactly_one_thing_to_look_for():
    [built] = wired(load([source(address=TAG)]))
    assert built.options["adapter"] == "hci0"
    assert built.options["enter_sightings"] == 3
    assert built.options["absent_after_seconds"] == 120.0
    with pytest.raises(configuration.ConfigError, match="exactly one of address"):
        load([source()])
    with pytest.raises(configuration.ConfigError, match="exactly one of address"):
        load([source(address=TAG, ibeacon_uuid=BEACON)])
    with pytest.raises(configuration.ConfigError, match="go with ibeacon_uuid"):
        load([source(address=TAG, ibeacon_major=3)])
    with pytest.raises(configuration.ConfigError, match="AA:BB"):
        load([source(address="aa:bb:cc:dd:ee:ff")])
    with pytest.raises(configuration.ConfigError, match="adapter"):
        load([source(address=TAG, adapter="usb0")])


def test_two_sources_may_not_watch_the_same_device():
    with pytest.raises(configuration.ConfigError, match="same device"):
        load([source(address=TAG), source(id="tag-2", address=TAG)])
    load([source(address=TAG), source(id="tag-2", ibeacon_uuid=BEACON)])


def test_scanning_is_not_a_capability_of_a_board_that_streams():
    """The radio is shared with the Wi-Fi that carries a stream, and that is not qualified."""
    with pytest.raises(configuration.ConfigError, match="camera-sensor node has no ble"):
        load([source(address=TAG)], profile="camera-sensor")
    with pytest.raises(configuration.ConfigError, match="sensor-presence node has no csi"):
        load([source(address=TAG), {"id": "cam-1", "kind": "csi"}])


def test_the_board_shares_one_scanner_for_its_sources():
    made = []
    hardware = drivers.Hardware(open_scanner=lambda adapter: made.append(adapter) or FakeScanner())
    first = drivers.build_one(load([source(address=TAG)])[0], hardware)
    second = drivers.build_one(load([source(id="tag-2", ibeacon_uuid=BEACON)])[0], hardware)
    assert isinstance(first, BlePresence) and first.scanner is second.scanner
    assert made == ["hci0"]
    hardware.close()

"""Sensor drivers against pretend hardware: what they report, and what they refuse to make up."""

import struct
import sys
import threading
import time
import tomllib
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentry_satellite import config as configuration
from sentry_satellite import drivers
from sentry_satellite.sensors import baseline
from sentry_satellite.sensors.adc import Ads1115, config_word
from sentry_satellite.sensors.bme280 import Bme280, Calibration, compensate
from sentry_satellite.sensors.board import Cpu, Temperature
from sentry_satellite.sensors.bounded import Bounded, Busy, Timeout
from sentry_satellite.sensors.digital import DigitalInput
from sentry_satellite.sensors.gpio import Edge, LineError
from sentry_satellite.sensors.i2c import BusError
from sentry_satellite.sensors.onewire import Ds18b20, parse
from sentry_satellite.sensors.periodic import Periodic, Sample, Unreadable

# -- helpers -----------------------------------------------------------------------------


class FakeLine:
    """A line whose future is a script: each step is (edges, level after them) or an error."""

    def __init__(self, level=False, steps=(), pending=0):
        self.current = level
        self.steps = deque(steps)
        self.pending = [Edge(True, 0)] * pending
        self.released = False

    def level(self):
        return self.current

    def wait(self, timeout):
        if self.steps:
            step = self.steps.popleft()
            if isinstance(step, Exception):
                raise step
            count, level = step
            self.pending = [Edge(level, 0)] * count
            self.current = level
            return True
        time.sleep(min(timeout, 0.005))
        return False

    def edges(self):
        pending, self.pending = self.pending, []
        return pending

    def release(self):
        self.released = True


def collect(driver, count, timeout=2.0):
    """Run a driver until it has said `count` things, then stop it."""
    seen = []
    done = threading.Event()

    def run():
        for reading in driver.read():
            seen.append(reading)
            if len(seen) >= count:
                done.set()
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    done.wait(timeout)
    time.sleep(0.05)  # anything extra that should not have been said
    driver.stop()
    thread.join(timeout=2)
    assert not thread.is_alive(), "the driver did not stop"
    return seen


def digital(line_or_factory, **options):
    factory = line_or_factory if callable(line_or_factory) else (lambda: line_or_factory)
    options.setdefault("debounce_ms", 1)
    options.setdefault("poll_seconds", 0.01)
    options.setdefault("retry_seconds", 0.01)
    return DigitalInput("pir-1", factory, **options)


# -- digital inputs ----------------------------------------------------------------------


def test_the_first_thing_a_pir_says_is_a_baseline_not_a_detection():
    line = FakeLine(level=True)
    readings = collect(digital(line), 2, timeout=0.2)
    assert [(r.value, r.initial) for r in readings] == [(True, True)]
    assert readings[0].kind == "sensor.motion"
    assert line.released


def test_bounce_that_ends_where_it_started_says_nothing():
    line = FakeLine(level=False, steps=[(5, False), (3, True), (4, True), (2, False)])
    readings = collect(digital(line), 3)
    assert [(r.value, r.initial) for r in readings] == [
        (False, True),
        (True, False),
        (False, False),
    ]


def test_an_active_low_contact_is_inverted():
    line = FakeLine(level=False, steps=[(1, True)])
    readings = collect(digital(line, active_high=False, event_kind="sensor.contact"), 2)
    assert [r.value for r in readings] == [True, False]
    assert {r.kind for r in readings} == {"sensor.contact"}


def test_edges_while_the_sensor_settles_are_not_news():
    line = FakeLine(level=False, pending=6)
    started = time.monotonic()
    readings = collect(digital(line, settle_seconds=0.1), 1)
    assert time.monotonic() - started >= 0.1
    assert [(r.value, r.initial) for r in readings] == [(False, True)]


def test_a_line_that_cannot_be_opened_is_unknown_not_false():
    attempts = []
    line = FakeLine(level=True)

    def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise LineError("line 17 on /dev/gpiochip0 is in use by lirc")
        return line

    driver = digital(factory)
    readings = collect(driver, 2)
    assert readings[0].quality == "unavailable" and readings[0].value is None
    assert readings[0].initial is False
    # Said once, even though it failed twice, then a fresh baseline once it worked.
    assert (readings[1].value, readings[1].initial, readings[1].quality) == (True, True, "valid")
    assert driver.error is None


def test_a_line_that_fails_while_watched_is_released_and_reopened():
    first = FakeLine(level=True, steps=[LineError("gone")])
    second = FakeLine(level=False)
    lines = deque([first, second])
    readings = collect(digital(lambda: lines.popleft()), 3)
    assert [(r.value, r.quality, r.initial) for r in readings] == [
        (True, "valid", True),
        (None, "unavailable", False),
        (False, "valid", True),
    ]
    assert first.released


def test_a_driver_repeats_its_state_as_a_baseline_on_request():
    driver = digital(FakeLine(level=True))
    assert baseline(driver) is None
    readings = []
    thread = threading.Thread(target=lambda: readings.extend(driver.read()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while baseline(driver) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    again = baseline(driver)
    driver.stop()
    thread.join(2)
    assert (again.value, again.initial) == (True, True)


def test_stop_is_prompt_even_with_a_long_settle():
    driver = digital(FakeLine(), settle_seconds=60)
    started = time.monotonic()
    assert collect(driver, 1, timeout=0.05) == []
    assert time.monotonic() - started < 1


# -- periodic sensors --------------------------------------------------------------------


def periodic(sample, **options):
    options.setdefault("interval_seconds", 0.01)
    options.setdefault("timeout_seconds", 0.2)
    return Periodic("temp-1", sample, event_kind="climate.temperature", unit="°C", **options)


def test_the_first_good_value_is_the_baseline():
    values = iter([20.5, 20.6])
    readings = collect(periodic(lambda: Sample(next(values, 20.7))), 2)
    assert [(r.value, r.initial, r.unit) for r in readings[:2]] == [
        (20.5, True, "°C"),
        (20.6, False, "°C"),
    ]


def test_a_failed_read_is_reported_once_and_never_as_a_number():
    script = iter(["ok", "crc", "crc", "crc", "ok"])

    def sample():
        if next(script, "ok") == "crc":
            raise Unreadable("CRC mismatch")
        return Sample(21.0)

    readings = collect(periodic(sample), 3)
    assert [(r.value, r.quality) for r in readings[:3]] == [
        (21.0, "valid"),
        (None, "unavailable"),
        (21.0, "valid"),
    ]
    assert readings[2].initial is False


def test_a_read_that_hangs_is_reported_and_not_doubled_up():
    release = threading.Event()
    calls = []

    def stuck():
        calls.append(1)
        release.wait(5)
        return Sample(1.0)

    driver = periodic(stuck, timeout_seconds=0.05)
    readings = collect(driver, 1)
    assert readings[0].quality == "unavailable"
    assert len(calls) == 1  # later attempts found it busy and did not start another
    release.set()


def test_bounded_calls_say_why_they_gave_up():
    gate = threading.Event()
    bounded = Bounded("probe")
    with pytest.raises(Timeout):
        bounded.call(lambda: gate.wait(5), 0.01)
    with pytest.raises(Busy):
        bounded.call(lambda: 1, 0.01)
    gate.set()
    time.sleep(0.05)
    assert bounded.call(lambda: 2, 1) == 2
    with pytest.raises(ZeroDivisionError):
        bounded.call(lambda: 1 / 0, 1)


# -- DS18B20 -----------------------------------------------------------------------------

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n"


def test_a_ds18b20_answer_is_read_in_thousandths():
    assert parse(GOOD) == 23.125
    assert parse(GOOD.replace("t=23125", "t=-1062")) == -1.062


@pytest.mark.parametrize(
    "text,reason",
    [
        (GOOD.replace("YES", "NO"), "CRC"),
        (GOOD.replace("t=23125", "t=85000"), "power-on"),
        (GOOD.replace("t=23125", "t=127000"), "outside"),
        (GOOD.replace("t=23125", "t=abc"), "unexpected"),
        (GOOD.splitlines()[0], "two lines"),
        ("", "two lines"),
    ],
)
def test_a_bad_ds18b20_answer_is_not_a_temperature(text, reason):
    with pytest.raises(Unreadable, match=reason):
        parse(text)


def test_a_probe_that_is_not_on_the_bus_is_unreadable(tmp_path):
    probe = Ds18b20("28-0123456789ab", root=tmp_path)
    with pytest.raises(Unreadable, match="not on the bus"):
        probe()
    (tmp_path / "28-0123456789ab").mkdir()
    (tmp_path / "28-0123456789ab" / "w1_slave").write_text(GOOD)
    assert probe().value == 23.125
    with pytest.raises(ValueError):
        Ds18b20("../../etc/passwd", root=tmp_path)


# -- BME280 ------------------------------------------------------------------------------

# The worked example from the Bosch datasheet, and humidity constants from a real part.
T = (27504, 26435, -1000)
P = (36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)
H = (75, 362, 0, 313, 50, 30)


def calibration_bytes():
    first = struct.pack("<HhhHhhhhhhhh", *T, *P) + b"\x00" + bytes([H[0]])
    e4 = (H[3] >> 4) & 0xFF
    e5 = ((H[4] & 0x0F) << 4) | (H[3] & 0x0F)
    e6 = (H[4] >> 4) & 0xFF
    second = struct.pack("<hB", H[1], H[2]) + bytes([e4, e5, e6]) + struct.pack("<b", H[5])
    return first, second


def raw(adc_p, adc_t, adc_h):
    return bytes(
        [
            (adc_p >> 12) & 0xFF,
            (adc_p >> 4) & 0xFF,
            (adc_p << 4) & 0xF0,
            (adc_t >> 12) & 0xFF,
            (adc_t >> 4) & 0xFF,
            (adc_t << 4) & 0xF0,
            adc_h >> 8,
            adc_h & 0xFF,
        ]
    )


def reference_humidity(adc_h, t_fine, h):
    """BME280_compensate_H_int32 from the datasheet, as an independent check."""
    h1, h2, h3, h4, h5, h6 = h
    v = t_fine - 76800
    v = ((((adc_h << 14) - (h4 << 20) - (h5 * v)) + 16384) >> 15) * (
        ((((((v * h6) >> 10) * (((v * h3) >> 11) + 32768)) >> 10) + 2097152) * h2 + 8192) >> 14
    )
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * h1) >> 4)
    v = max(0, min(v, 419430400))
    return (v >> 12) / 1024


def test_the_compensation_matches_the_datasheet():
    calibration = Calibration.parse(*calibration_bytes())
    assert calibration.t == T and calibration.p == P and calibration.h == H
    measured = compensate(raw(415148, 519888, 30000), calibration)
    assert measured.temperature == pytest.approx(25.08, abs=0.01)
    assert measured.pressure == pytest.approx(1006.5327, abs=0.01)
    t1, t2, t3 = T
    var1 = (((519888 >> 3) - (t1 << 1)) * t2) >> 11
    var2 = (((((519888 >> 4) - t1) * ((519888 >> 4) - t1)) >> 12) * t3) >> 14
    assert measured.humidity == pytest.approx(reference_humidity(30000, var1 + var2, H), abs=0.05)


def test_negative_humidity_constants_survive_parsing():
    negative = (75, 362, 0, -5, -20, -1)
    first, _ = calibration_bytes()
    e4 = (negative[3] >> 4) & 0xFF
    e5 = ((negative[4] & 0x0F) << 4) | (negative[3] & 0x0F)
    e6 = (negative[4] >> 4) & 0xFF
    second = struct.pack("<hB", 362, 0) + bytes([e4, e5, e6]) + struct.pack("<b", -1)
    assert Calibration.parse(first, second).h == negative


def test_a_skipped_measurement_is_not_a_value():
    calibration = Calibration.parse(*calibration_bytes())
    with pytest.raises(Unreadable):
        compensate(raw(415148, 0x80000, 30000), calibration)
    partial = compensate(raw(0x80000, 519888, 0x8000), calibration)
    assert partial.pressure is None and partial.humidity is None


class FakeBme280Bus:
    def __init__(self, chip=0x60):
        first, second = calibration_bytes()
        self.registers = {0xD0: bytes([chip]), 0x88: first, 0xE1: second}
        self.registers[0xF7] = raw(415148, 519888, 30000)
        self.status = deque([0x08, 0x00])
        self.writes = []
        self.fail = False
        self.lock = threading.Lock()

    def read(self, register, length):
        if self.fail:
            raise BusError("i2c-1 0x76 did not answer: Remote I/O error")
        if register == 0xF3:
            return bytes([self.status.popleft() if self.status else 0])
        return self.registers[register][:length]

    def write(self, register, data):
        if self.fail:
            raise BusError("i2c-1 0x76 did not answer")
        self.writes.append((register, data))

    def close(self):
        pass


def test_three_quantities_from_one_part_take_one_measurement():
    bus = FakeBme280Bus()
    clock = [0.0]
    part = Bme280(bus, lock=bus.lock, clock=lambda: clock[0], sleep=lambda s: None)
    values = {q: part.sampler(q)().value for q in ("temperature", "humidity", "pressure")}
    assert values["temperature"] == 25.08
    assert values["pressure"] == pytest.approx(1006.53, abs=0.01)
    assert 0 < values["humidity"] < 100
    assert [w for w in bus.writes if w[0] == 0xF4] == [(0xF4, b"\x25")]
    clock[0] = 2.0
    part.sampler("temperature")()
    assert len([w for w in bus.writes if w[0] == 0xF4]) == 2


def test_a_part_that_is_not_a_bme280_or_does_not_answer_is_unreadable():
    bus = FakeBme280Bus(chip=0x58)
    part = Bme280(bus, lock=bus.lock, sleep=lambda s: None)
    with pytest.raises(Unreadable, match="not a BME280"):
        part.sampler("temperature")()
    bus = FakeBme280Bus()
    bus.fail = True
    part = Bme280(bus, lock=bus.lock, sleep=lambda s: None)
    with pytest.raises(Unreadable, match="did not answer"):
        part.sampler("humidity")()
    with pytest.raises(ValueError):
        part.sampler("altitude")


def test_a_measurement_that_never_finishes_is_unreadable():
    bus = FakeBme280Bus()
    bus.status = deque([0x08] * 1000)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    part = Bme280(bus, lock=bus.lock, clock=lambda: clock[0], sleep=sleep)
    with pytest.raises(Unreadable, match="did not finish"):
        part.sampler("temperature")()


# -- ADS1115 -----------------------------------------------------------------------------


class FakeAdcBus:
    def __init__(self, value, ready=True):
        self.value = value
        self.ready = ready
        self.writes = []
        self.lock = threading.Lock()

    def read(self, register, length):
        if register == 0x01:
            return (0x8000 if self.ready else 0).to_bytes(2, "big")
        return self.value.to_bytes(2, "big", signed=True)

    def write(self, register, data):
        self.writes.append((register, data))

    def close(self):
        pass


def adc(bus, **options):
    return Ads1115(bus, lock=bus.lock, sleep=lambda s: None, **options)


def test_the_converter_is_asked_for_one_single_ended_conversion():
    assert config_word(0) == 0xC383
    assert config_word(3) == 0xF383
    bus = FakeAdcBus(12288)
    assert adc(bus, channel=1)().value == pytest.approx(1.536 / 3.3, abs=1e-4)
    assert bus.writes == [(0x01, (0xD383).to_bytes(2, "big"))]
    assert adc(bus, channel=1, output="volts")().value == pytest.approx(1.536)
    assert adc(bus, channel=0, output="volts").unit == "V"
    assert adc(bus, channel=0).unit == "ratio"


def test_a_reading_the_divider_cannot_produce_is_unreadable():
    with pytest.raises(Unreadable, match="outside"):
        adc(FakeAdcBus(-4000), channel=0)()
    with pytest.raises(Unreadable, match="outside"):
        adc(FakeAdcBus(32767), channel=0, reference_volts=3.3)()


def test_a_conversion_that_never_finishes_is_unreadable():
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    converter = Ads1115(
        FakeAdcBus(100, ready=False),
        lock=threading.Lock(),
        channel=0,
        clock=lambda: clock[0],
        sleep=sleep,
    )
    with pytest.raises(Unreadable, match="did not finish"):
        converter()


def test_bad_converter_settings_are_refused():
    for options in (
        {"channel": 4},
        {"channel": 0, "output": "lux"},
        {"channel": 0, "reference_volts": 5},
    ):
        with pytest.raises(ValueError):
            adc(FakeAdcBus(0), **options)


# -- configuration and building ----------------------------------------------------------

HEAD = """
[node]
id = "zero-entrance"
profile = "camera-sensor"

[hub]
mqtt_host = "192.168.11.10"

[tls]
ca_file = "/ca.crt"
cert_file = "/node.crt"
key_file = "/node.key"
"""


def sources(*tables):
    return configuration.parse(
        tomllib.loads(HEAD + "\n".join(tables)), identity_file=Path("/nowhere.json")
    )


PIR = '[[sources]]\nid = "pir-1"\nkind = "gpio"\nline_numbering = "bcm"\nline = 17\n'
TEMP = '[[sources]]\nid = "temp"\nkind = "bme280"\nmeasure = "temperature"\n'
HUM = '[[sources]]\nid = "hum"\nkind = "bme280"\nmeasure = "humidity"\n'
LIGHT = '[[sources]]\nid = "light"\nkind = "adc"\nchannel = 0\n'
PROBE = '[[sources]]\nid = "probe"\nkind = "onewire"\ndevice = "28-0123456789ab"\n'


def test_defaults_are_filled_in_and_written_down():
    config = sources(PIR, TEMP, LIGHT, PROBE)
    pir = config.source("pir-1").options
    assert pir == {
        "enabled": True,
        "chip": "/dev/gpiochip0",
        "line_numbering": "bcm",
        "line": 17,
        "active_high": True,
        "bias": "disabled",
        "debounce_ms": 50,
        "settle_seconds": 0.0,
        "event_kind": "sensor.motion",
    }
    assert config.source("temp").options["address"] == 0x76
    assert config.source("light").options["event_kind"] == "light.level"
    assert config.source("probe").options["interval_seconds"] == 30.0


@pytest.mark.parametrize(
    "tables,problem",
    [
        (['[[sources]]\nid = "pir-1"\nkind = "gpio"\nline = 17\n'], "missing line_numbering"),
        ([PIR.replace('"bcm"', '"board"')], "must be one of bcm"),
        ([PIR.replace("17", "60")], "from 0 to 53"),
        ([PIR.replace("17", '"17"')], "line must be int"),
        ([PIR + "active_high = 1\n"], "active_high must be bool"),
        ([PIR + "debounce = 5\n"], "does not take debounce"),
        ([PIR + 'event_kind = "Motion"\n'], "event kind"),
        ([PIR, PIR.replace('"pir-1"', '"pir-2"')], "both use BCM line 17"),
        ([TEMP, PIR.replace("17", "2")], "belongs to i2c-1"),
        ([PROBE, PIR.replace("17", "4")], "belongs to the 1-Wire bus"),
        ([PIR.replace("17", "19"), '[[sources]]\nid = "mic"\nkind = "microphone"\n'], "I2S"),
        ([TEMP, TEMP.replace('"temp"', '"temp-2"')], "same temperature"),
        ([TEMP, LIGHT + "address = 0x76\n"], "must be one of"),
        ([LIGHT, LIGHT.replace('"light"', '"light-2"')], "same 0"),
        ([PROBE, PROBE.replace('"probe"', '"probe-2"')], "same probe"),
        ([PROBE.replace("28-0123456789ab", "10-0123456789ab")], "28-0123456789ab"),
        ([LIGHT + "reference_volts = 5\n"], "from 0.1 to 4.096"),
    ],
)
def test_a_source_that_cannot_work_is_refused_at_validate(tables, problem):
    with pytest.raises(configuration.ConfigError, match=problem):
        sources(*tables)


# -- what the board says about itself ----------------------------------------------------


STAT = "cpu  {user} 0 {system} {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\nintr 99\n"


def test_the_board_temperature_is_read_from_the_thermal_zone(tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("47216\n")
    assert Temperature(zone)().value == 47.2


def test_a_thermal_zone_that_answers_nonsense_is_unavailable_not_a_guess(tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("warm\n")
    with pytest.raises(Unreadable):
        Temperature(zone)()
    zone.write_text("900000\n")  # 900 °C: a unit that is not thousandths, or a broken file
    with pytest.raises(Unreadable):
        Temperature(zone)()
    with pytest.raises(Unreadable):
        Temperature(tmp_path / "no-such-zone")()


def test_the_processor_share_is_what_happened_between_two_readings(tmp_path):
    """The counters are totals since boot, so one reading of them says nothing at all."""
    stat = tmp_path / "stat"
    stat.write_text(STAT.format(user=100, system=0, idle=900))
    cpu = Cpu(stat)
    stat.write_text(STAT.format(user=140, system=10, idle=950))
    assert cpu().value == 50.0  # 50 busy ticks out of the 100 that passed
    stat.write_text(STAT.format(user=140, system=10, idle=1050))
    assert cpu().value == 0.0
    assert cpu().value == 0.0  # asked again before the clock moved: the last interval stands


def test_every_board_reports_its_temperature_and_its_occupancy_unasked():
    """Neither needs a wire, so a node reports both without being configured for them."""
    config = sources(PIR)
    assert [(s.id, s.options["measure"]) for s in config.sources if s.kind == "board"] == [
        ("board-temperature", "temperature"),
        ("board-cpu", "cpu"),
    ]
    built = {d.source_id: d for d in drivers.build(config, drivers.Hardware(open_line=FakeLine))}
    assert (built["board-temperature"]._kind, built["board-temperature"]._unit) == (
        "board.temperature",
        "°C",
    )
    assert (built["board-cpu"]._kind, built["board-cpu"]._unit) == ("board.cpu", "%")


def test_a_node_that_names_the_board_itself_is_left_to_say_what_it_means():
    config = sources(
        '[[sources]]\nid = "board-temperature"\nkind = "board"\n'
        'measure = "temperature"\ninterval_seconds = 300\nenabled = false\n'
    )
    board = [source for source in config.sources if source.kind == "board"]
    assert [(s.id, s.options["interval_seconds"], s.enabled) for s in board] == [
        ("board-temperature", 300.0, False),
        ("board-cpu", 30.0, True),
    ]


def named(thing) -> str:
    if isinstance(thing, dict):
        return str(thing.get("id", ""))
    return str(getattr(thing, "source_id", None) or getattr(thing, "id", thing))


def wired(things):
    """What the configuration asked for, without the two readings every board takes."""
    return [thing for thing in things if not named(thing).startswith("board-")]


def test_a_disabled_source_claims_nothing_and_is_not_built():
    config = sources(PIR, PIR.replace('"pir-1"', '"pir-2"') + "enabled = false\n")
    assert [source.enabled for source in wired(config.sources)] == [True, False]
    built = drivers.build(config, drivers.Hardware(open_line=lambda *a, **k: FakeLine()))
    assert [driver.source_id for driver in wired(built)] == ["pir-1"]
    assert "pir-2 (gpio, disabled)" in configuration.summary(config)["sources"]


def test_drivers_share_one_part_and_open_nothing_until_they_read():
    opened = []

    def open_i2c(bus, address):
        opened.append((bus, address))
        return FakeBme280Bus()

    hardware = drivers.Hardware(open_i2c=open_i2c, open_line=lambda *a, **k: FakeLine())
    built = drivers.build(sources(PIR, TEMP, HUM, LIGHT, PROBE), hardware)
    assert [type(d).__name__ for d in wired(built)] == [
        "DigitalInput",
        "Periodic",
        "Periodic",
        "Periodic",
        "Periodic",
    ]
    assert opened == [(1, 0x76), (1, 0x48)]  # a lazy file handle each, no I/O yet
    assert hardware.bme280(1, 0x76) is hardware.bme280(1, 0x76)


def test_the_gpio_line_is_requested_with_the_configured_settings():
    requested = []

    def open_line(chip, line, *, consumer, bias):
        requested.append((chip, line, consumer, bias))
        return FakeLine(level=True)

    config = sources(PIR + 'bias = "pull_down"\n')
    [driver] = wired(drivers.build(config, drivers.Hardware(open_line=open_line)))
    collect(driver, 1)
    assert requested == [("/dev/gpiochip0", 17, "sentry-satellite:pir-1", "pull_down")]


def test_kinds_without_a_driver_still_say_which_increment_brings_them():
    config = sources('[[sources]]\nid = "cam"\nkind = "uvc"\n')
    with pytest.raises(drivers.UnsupportedSource, match="USB camera"):
        drivers.build(config)


def test_a_driver_bug_is_reported_as_unavailable_not_as_a_crash():
    readings = collect(periodic(lambda: {}["missing"]), 1)
    assert readings[0].quality == "unavailable"


def test_readings_carry_the_time_they_were_taken():
    before = datetime.now(UTC)
    [reading] = collect(periodic(lambda: Sample(1.0), interval_seconds=10), 1)
    assert before <= reading.occurred_at <= datetime.now(UTC)


# -- the libgpiod adapter, against a module that behaves like the real one ---------------


def fake_gpiod(monkeypatch, pending):
    """A `gpiod` whose read blocks, as the real one does, when nothing is queued."""
    import enum
    import types

    line = types.ModuleType("gpiod.line")
    line.Bias = enum.Enum("Bias", "AS_IS DISABLED PULL_UP PULL_DOWN")
    line.Direction = enum.Enum("Direction", "INPUT OUTPUT")
    line.Edge = enum.Enum("Edge", "NONE RISING FALLING BOTH")
    line.Value = enum.Enum("Value", "INACTIVE ACTIVE")
    edge_event = types.ModuleType("gpiod.edge_event")

    class EdgeEvent:
        Type = enum.Enum("Type", "RISING_EDGE FALLING_EDGE")

        def __init__(self, rising):
            self.event_type = EdgeEvent.Type.RISING_EDGE if rising else EdgeEvent.Type.FALLING_EDGE
            self.timestamp_ns = 1

    edge_event.EdgeEvent = EdgeEvent

    class Request:
        released = False

        def get_value(self, offset):
            return line.Value.ACTIVE

        def wait_edge_events(self, timeout):
            return bool(pending)

        def read_edge_events(self):
            if not pending:
                raise AssertionError("read_edge_events would block here")
            events = [EdgeEvent(rising) for rising in pending[:1]]
            del pending[:1]
            return events

        def release(self):
            Request.released = True

    gpiod = types.ModuleType("gpiod")
    gpiod.LineSettings = lambda **settings: settings
    gpiod.request_lines = lambda chip, consumer, config: Request()
    gpiod.line = line
    gpiod.edge_event = edge_event
    for name, module in (("gpiod", gpiod), ("gpiod.line", line), ("gpiod.edge_event", edge_event)):
        monkeypatch.setitem(sys.modules, name, module)
    return Request


def test_draining_edges_never_waits_for_one_that_is_not_there(monkeypatch):
    from sentry_satellite.sensors.gpio import open_input

    pending = [True, False]
    request = fake_gpiod(monkeypatch, pending)
    line = open_input("/dev/gpiochip0", 17, consumer="t", bias="disabled")
    assert line.level() is True
    assert [edge.rising for edge in line.edges()] == [True, False]
    assert line.edges() == []
    line.release()
    assert request.released

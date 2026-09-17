"""The microphone on a satellite: one capture, what it says about loudness, and the stream."""

import json
import math
import struct
import sys
import time
from threading import Thread

import pytest
from conftest import CONTRACTS
from test_agent import build, grant, until
from test_video import Hub, last_ack, video

from sentry_satellite import commands, drivers, protocol
from sentry_satellite.audio import blocks
from sentry_satellite.audio.capture import Capture, Chunk, Tap
from sentry_satellite.audio.publisher import AudioPublisher
from sentry_satellite.audio.source import Activity, AlsaMicrophone, capture_argv, dbfs
from sentry_satellite.camera.publisher import Stream
from sentry_satellite.camera.source import CsiCamera
from sentry_satellite.config import ConfigError, Source, check_conflicts, check_options

STREAM = Stream(stream_id="stream-0001", source_id="mic-1", port=8555, token="t" * 32, hub_epoch=7)
OPTIONS = check_options("microphone", {}, "mic-1")
LISTENING = check_options("microphone", {"activity": True}, "mic-1")

# A stand-in for arecord: 100 ms of a loud or a quiet tone, ten times a second.
TONE = """
import math, struct, sys, time
amplitude = {amplitude}
block = b"".join(
    struct.pack("<h", int(amplitude * math.sin(i / 8))) for i in range(1600)
)
while True:
    sys.stdout.buffer.write(block)
    sys.stdout.flush()
    time.sleep(0.02)
"""
LOUD = TONE.format(amplitude=20000)
QUIET = TONE.format(amplitude=10)
BRIEF = """
import sys
sys.stdout.buffer.write(b"\\0" * 3200)
sys.stdout.flush()
sys.stderr.write("microphone unplugged\\n")
"""
SILENT = "import time; time.sleep(60)"


def program(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def capture(code: str, **options) -> Capture:
    options.setdefault("retry_seconds", 0.05)
    return Capture("mic-1", lambda: program(code), **options)


def pcm(amplitude: int, samples: int = blocks.BLOCK_SAMPLES) -> bytes:
    return b"".join(struct.pack("<h", amplitude if i % 2 else -amplitude) for i in range(samples))


# -- the format --------------------------------------------------------------------------


@pytest.mark.skipif(not CONTRACTS.is_dir(), reason="the contracts are not beside us")
def test_blocks_are_packed_the_way_the_hub_unpacks_them():
    contract = json.loads((CONTRACTS / "audio.json").read_text())
    assert contract["magic"] == blocks.MAGIC.decode()
    assert contract["version"] == blocks.VERSION
    assert contract["struct"] == blocks.HEADER.format
    assert contract["header_bytes"] == blocks.HEADER.size
    assert contract["sample_rate"] == blocks.RATE
    assert contract["channels"] == blocks.CHANNELS
    assert contract["block_samples"] == blocks.BLOCK_SAMPLES
    assert contract["max_block_samples"] * blocks.SAMPLE_BYTES == 2 * blocks.BLOCK_BYTES
    assert contract["flags"] == {"gap": blocks.FLAG_GAP}
    packed = blocks.pack(3, 4800, 99, pcm(5), gap=True)
    magic, version, flags, reserved, sequence, first, captured, count = struct.unpack_from(
        contract["struct"], packed
    )
    assert (magic, version, flags, reserved) == (b"SMA1", 1, 1, 0)
    assert (sequence, first, captured, count) == (3, 4800, 99, 1600)
    assert len(packed) == contract["header_bytes"] + 3200


@pytest.mark.parametrize("size", [0, 3, 6402])
def test_a_block_carries_whole_samples_and_not_too_many(size):
    with pytest.raises(ValueError):
        blocks.pack(0, 0, 0, b"\0" * size, gap=False)


# -- loudness ----------------------------------------------------------------------------


def test_silence_and_full_scale_are_the_ends_of_the_scale():
    assert dbfs(b"\0" * 3200) == -96.0
    assert dbfs(pcm(32767)) == pytest.approx(0.0, abs=0.1)
    assert dbfs(pcm(3277)) == pytest.approx(-20.0, abs=0.1)
    assert dbfs(b"") == -96.0


def test_activity_needs_loudness_for_long_enough_and_quiet_for_longer():
    activity = Activity(threshold_dbfs=-35, min_seconds=0.3, hold_seconds=1.0)
    assert activity.step(-20, 0.1) is None
    assert activity.step(-60, 0.1) is None  # a click is not activity
    assert [activity.step(-20, 0.1) for _ in range(3)] == [None, None, True]
    assert activity.step(-20, 0.1) is None
    # Between the threshold and the hysteresis below it: neither loud nor quiet.
    assert all(activity.step(-38, 0.1) is None for _ in range(30))
    assert [activity.step(-60, 0.25) for _ in range(4)] == [None, None, None, False]
    assert activity.active is False


# -- configuration ---------------------------------------------------------------------


def test_a_microphone_defaults_to_the_one_capture_format_and_no_activity():
    assert OPTIONS == {
        "enabled": True,
        "device": "default",
        "interface": "i2s",
        "activity": False,
        "activity_threshold_dbfs": -35.0,
        "activity_min_seconds": 0.3,
        "activity_hold_seconds": 3.0,
        "event_kind": "audio.activity",
    }
    argv = capture_argv({**OPTIONS, "device": "plughw:CARD=mic,DEV=0"}, program="/usr/bin/x")
    assert argv[0] == "/usr/bin/x"
    assert argv[argv.index("--device") + 1] == "plughw:CARD=mic,DEV=0"
    assert argv[argv.index("--rate") + 1] == "16000"
    assert argv[argv.index("--channels") + 1] == "1"
    assert argv[argv.index("--format") + 1] == "S16_LE"


@pytest.mark.parametrize(
    "options, problem",
    [
        ({"device": "hw:0 --file /etc/shadow"}, "ALSA device"),
        ({"device": "-D"}, "ALSA device"),
        ({"rate": 48000}, "does not take"),
        ({"interface": "spi"}, "interface"),
        ({"activity_threshold_dbfs": 0}, "threshold"),
        ({"activity": "yes"}, "bool"),
    ],
)
def test_a_microphone_outside_what_is_supported_is_refused(options, problem):
    with pytest.raises(ConfigError, match=problem):
        check_options("microphone", options, "mic-1")


def test_two_microphones_cannot_share_a_device_and_usb_leaves_the_i2s_pins_free():
    one = Source("mic-1", "microphone", OPTIONS)
    two = Source("mic-2", "microphone", OPTIONS)
    with pytest.raises(ConfigError, match="both capture"):
        check_conflicts([one, two])
    usb = check_options("microphone", {"interface": "usb", "device": "plughw:1,0"}, "mic-3")
    pin = check_options("gpio", {"line_numbering": "bcm", "line": 19}, "button")
    check_conflicts([Source("mic-3", "microphone", usb), Source("button", "gpio", pin)])
    with pytest.raises(ConfigError, match="I2S"):
        check_conflicts([one, Source("button", "gpio", pin)])


def test_a_microphone_is_a_driver_this_agent_has(monkeypatch):
    assert "microphone" in drivers.SUPPORTED
    built = drivers.build_one(Source("mic-1", "microphone", OPTIONS), drivers.Hardware())
    assert isinstance(built, AlsaMicrophone)
    monkeypatch.setattr("shutil.which", lambda name: None)
    missing = AlsaMicrophone("mic-1", OPTIONS)
    assert "not installed" in missing.error
    with pytest.raises(RuntimeError):
        missing.capture_argv()


# -- the capture -------------------------------------------------------------------------


def test_a_reader_that_falls_behind_loses_the_oldest_and_is_told():
    tap = Tap(limit=2)
    for index in range(4):
        tap.put(Chunk(index * 1600, index, b"\0\0"))
    assert tap.dropped == 2
    first = tap.get(0)
    assert (first.first_sample, first.gap) == (3200, True)
    assert tap.get(0).gap is False
    assert tap.get(0) is None


def test_one_capture_feeds_every_reader_and_ends_with_the_last():
    shared = capture(LOUD)
    one, two = shared.subscribe(), shared.subscribe()
    try:
        firsts = [one.get(2), one.get(2)]
        assert [c.first_sample for c in firsts] == [0, 1600]
        assert len(firsts[0].pcm) == blocks.BLOCK_BYTES
        assert two.get(2).first_sample == 0
        assert shared.starts == 1
    finally:
        shared.unsubscribe(one)
        shared.unsubscribe(two)
    assert until(lambda: not shared.running)
    again = shared.subscribe()
    try:
        assert again.get(2) is not None and shared.starts == 2
    finally:
        shared.stop()


def test_a_capture_that_dies_is_started_again_and_the_lost_time_shows():
    shared = capture(BRIEF, retry_seconds=0.3)
    tap = shared.subscribe()
    try:
        first = tap.get(2)
        assert first.first_sample == 0
        assert until(lambda: tap.error and "unplugged" in tap.error)
        second = tap.get(2)
        # The program was away for at least the retry wait: that much sound is missing.
        assert second.first_sample >= 1600 + int(0.3 * blocks.RATE)
    finally:
        shared.stop()
    assert not shared.running


def test_a_silent_microphone_is_an_error_not_a_hang():
    shared = capture(SILENT, stall_seconds=0.3, retry_seconds=5)
    tap = shared.subscribe()
    try:
        assert until(lambda: shared.error and "said nothing" in shared.error)
    finally:
        started = time.monotonic()
        shared.stop()
        assert time.monotonic() - started < 3
    assert tap.closed


# -- the driver --------------------------------------------------------------------------


def microphone(code: str, options=LISTENING) -> AlsaMicrophone:
    made = AlsaMicrophone("mic-1", options, program=sys.executable)
    made.capture = capture(code)
    return made


def test_a_microphone_without_activity_opens_nothing():
    quiet = microphone(LOUD, OPTIONS)
    readings = []
    thread = Thread(target=lambda: readings.extend(quiet.read()))
    thread.start()
    time.sleep(0.2)
    assert not quiet.capture.running and quiet.current() is None
    quiet.stop()
    thread.join(2)
    assert not thread.is_alive() and readings == []


def test_loud_sound_is_reported_as_activity_and_nothing_else():
    loud = microphone(LOUD)
    assert loud.current().value is False and loud.current().initial
    readings = []
    thread = Thread(target=lambda: readings.extend(loud.read()))
    thread.start()
    try:
        assert until(lambda: readings)
        [reading] = readings
        assert (reading.kind, reading.value, reading.initial) == ("audio.activity", True, False)
        assert reading.unit is None and loud.level_dbfs > -10
        assert loud.current().value is True
    finally:
        loud.stop()
        thread.join(3)
    assert not thread.is_alive() and not loud.capture.running


def test_quiet_sound_reports_nothing():
    quiet = microphone(QUIET)
    readings = []
    thread = Thread(target=lambda: readings.extend(quiet.read()))
    thread.start()
    try:
        assert until(lambda: quiet.level_dbfs is not None)
        time.sleep(0.5)
        assert readings == [] and quiet.level_dbfs < -60
    finally:
        quiet.stop()
        thread.join(3)


# -- the stream --------------------------------------------------------------------------


def publisher(hub: Hub, shared: Capture, seconds: float = 30.0, **options) -> AudioPublisher:
    options.setdefault("retry_seconds", 0.05)
    options.setdefault("max_retry_seconds", 0.2)
    return AudioPublisher(
        STREAM, shared, hub.connect, expires_at=time.monotonic() + seconds, **options
    )


def unpack(data: bytes) -> list[tuple]:
    found = []
    while len(data) >= blocks.HEADER.size:
        header = blocks.HEADER.unpack_from(data)
        end = blocks.HEADER.size + header[-1] * 2
        found.append(header)
        data = data[end:]
    return found


def test_sound_introduces_itself_and_then_carries_numbered_blocks():
    hub, shared = Hub(), capture(LOUD)
    stream = publisher(hub, shared)
    stream.start()
    try:
        assert until(lambda: stream.blocks_sent >= 5)
        assert stream.state == "live"
    finally:
        stream.stop()
        assert stream.join(5)
    assert hub.headers == [
        {
            "schema_version": 1,
            "kind": "audio",
            "stream_id": "stream-0001",
            "source_id": "mic-1",
            "token": "t" * 32,
        }
    ]
    assert until(lambda: hub.received)
    sent = unpack(hub.received[0])
    assert [header[4] for header in sent] == list(range(len(sent)))
    assert [header[5] for header in sent][:2] == [0, 1600]
    assert all(header[0] == b"SMA1" and header[-1] == 1600 for header in sent)
    assert until(lambda: not shared.running)


def test_a_refused_sound_stream_is_not_retried():
    hub, shared = Hub({"ok": False, "detail": "wrong kind"}), capture(LOUD)
    stream = publisher(hub, shared)
    stream.start()
    assert stream.join(5)
    assert stream.state == "refused" and "wrong kind" in stream.error
    assert not shared.running


def test_a_microphone_that_stops_interrupts_the_stream_and_it_comes_back():
    hub, shared = Hub(), capture(SILENT, stall_seconds=10)
    stream = publisher(hub, shared, stall_seconds=0.3)
    stream.start()
    try:
        assert until(lambda: stream.interruptions >= 1)
        assert "no sound" in stream.last_interruption
        assert until(lambda: len(hub.headers) >= 2)
    finally:
        stream.stop()
        assert stream.join(5)
        shared.stop()


def test_a_hub_that_does_not_read_ends_the_connection():
    hub, shared = Hub(read=False), capture(LOUD)
    stream = publisher(hub, shared, send_seconds=0.3)
    stream.start()
    try:
        assert until(lambda: stream.interruptions >= 1, timeout=10)
        assert "fast enough" in stream.last_interruption
    finally:
        stream.stop()
        assert stream.join(5)
        shared.stop()


# -- the agent ---------------------------------------------------------------------------


def sound(action="audio_start", **changes) -> dict:
    return video(action, **{"source_id": "mic-1", **changes})


def ask(transport, **message):
    transport.deliver(protocol.topic("zero-entrance", "commands"), sound(**message))


def test_a_sound_command_is_checked_like_a_video_one():
    command = commands.parse(sound(), node_id="zero-entrance")
    assert (command.action, command.source_id) == ("audio_start", "mic-1")
    with pytest.raises(commands.CommandError, match="token"):
        commands.parse(sound(token="short"), node_id="zero-entrance")
    with pytest.raises(commands.CommandError, match="only its id"):
        commands.parse(sound("audio_stop"), node_id="zero-entrance")


def microphone_agent(hub: Hub, code: str = LOUD):
    mic = microphone(code, OPTIONS)
    camera = CsiCamera("camera-1", check_options("csi", {}, "c"), program="/bin/true")
    agent, transport = build([mic, camera])
    agent.connect_media = hub.connect
    agent.audio_publisher = lambda *a, **k: AudioPublisher(
        *a, retry_seconds=0.05, max_retry_seconds=0.2, **k
    )
    agent.start()
    return agent, transport, mic


def test_a_granted_microphone_streams_until_the_grant_is_taken_back():
    hub = Hub()
    agent, transport, mic = microphone_agent(hub)
    try:
        ask(transport)
        assert until(lambda: last_ack(transport, "v-1"))
        assert "needs a current grant" in last_ack(transport, "v-1")["detail"]
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport, command_id="v-2")
        assert until(lambda: last_ack(transport, "v-2"))
        assert last_ack(transport, "v-2")["outcome"] == "applied"
        assert until(lambda: agent._videos["mic-1"].blocks_sent >= 3)
        assert hub.headers[0]["kind"] == "audio"
        assert until(
            lambda: any(
                (h["sources"]["mic-1"].get("audio") or {}).get("state") == "live"
                for h in transport.on("health")
            ),
            timeout=5,
        )
        grant(transport, agent, command_id="c-2", action="revoke")
        assert until(lambda: agent._videos == {})
        assert until(lambda: not mic.capture.running)
    finally:
        agent.stop()
    assert agent.stopped_cleanly


def test_a_microphone_is_not_a_camera_and_a_camera_is_not_a_microphone():
    hub = Hub()
    agent, transport, _ = microphone_agent(hub)
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport, source_id="camera-1")
        assert until(lambda: last_ack(transport, "v-1"))
        assert "not a microphone" in last_ack(transport, "v-1")["detail"]
        transport.deliver(
            protocol.topic("zero-entrance", "commands"), video(command_id="v-2", source_id="mic-1")
        )
        assert until(lambda: last_ack(transport, "v-2"))
        assert "not a camera" in last_ack(transport, "v-2")["detail"]
        # A video command cannot stop a sound stream, even with its id.
        ask(transport, command_id="v-3")
        assert until(lambda: "mic-1" in agent._videos)
        stop = dict(source_id=None, port=None, token=None, duration_seconds=None)
        transport.deliver(
            protocol.topic("zero-entrance", "commands"),
            video("video_stop", command_id="v-4", **stop),
        )
        assert until(lambda: last_ack(transport, "v-4"))
        assert "mic-1" in agent._videos
        ask(transport, command_id="v-5", action="audio_stop", **stop)
        assert until(lambda: agent._videos == {})
    finally:
        agent.stop()


def test_stopping_the_agent_ends_the_capture():
    hub = Hub()
    agent, transport, mic = microphone_agent(hub)
    grant(transport, agent)
    assert until(lambda: agent._grant is not None)
    ask(transport)
    assert until(lambda: mic.capture.running)
    agent.stop()
    assert agent.stopped_cleanly
    assert until(lambda: not mic.capture.running)


def test_the_level_is_measured_the_same_way_whatever_the_amplitude():
    for amplitude in (100, 1000, 10000):
        expected = 20 * math.log10(amplitude / 32768)
        assert dbfs(pcm(amplitude)) == pytest.approx(expected, abs=0.1)

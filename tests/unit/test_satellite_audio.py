"""Sound from satellites: the block format, the microphone around it, and what hears it.

The gateway is tested over real mutual TLS with the certificates the video tests make;
recording with the real ffmpeg where there is one.
"""

import dataclasses
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sentry_mode.audio import blocks
from sentry_mode.audio.blocks import BLOCK_SAMPLES, Block, BlockError, Reassembler, pack
from sentry_mode.audio.remote import MAX_LISTENERS, MicrophoneTable, RemoteMicrophone
from sentry_mode.core.errors import HardwareError
from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.service import SatelliteService
from sentry_mode.sentry.config import (
    ARMABLE_TRIGGERS,
    AudioAction,
    AudioEventTrigger,
    RuleV2,
    SentryConfigV2,
    TTSAction,
)
from sentry_mode.sentry.engine import ECHO_SECONDS
from sentry_mode.sentry.resources import ResourcePlanner
from sentry_mode.sources.manager import SourceManager
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.media_gateway import GatewayError
from sentry_mode.vision.recording import Captures

sys.path.insert(0, str(Path(__file__).parent))  # the fixtures below live beside this file

from test_correlation import engine, event, log  # noqa: E402, F401 - the engine fixture
from test_satellite_video import (  # noqa: E402, F401 - fixtures
    MEDIA,
    Ends,
    Sink,
    Transport,
    dial,
    gateway,
    introduce,
    nodes_for,
    pki,
    say_hello,
    tls_config,
    until,
)

FFMPEG = shutil.which("ffmpeg")
MIC = "zero-entrance.mic-1"


def block(sequence, first=None, samples=BLOCK_SAMPLES, *, flags=0, fill=1):
    first = sequence * BLOCK_SAMPLES if first is None else first
    pcm = fill.to_bytes(2, "little", signed=True) * samples
    return pack(Block(sequence, first, 1_000 + sequence, flags, pcm))


# -- the format --------------------------------------------------------------------------


def test_blocks_in_order_come_out_as_they_went_in():
    joined = Reassembler()
    out = joined.feed(block(0) + block(1, fill=2))
    assert out == [b"\x01\x00" * BLOCK_SAMPLES, b"\x02\x00" * BLOCK_SAMPLES]
    assert joined.status()["blocks"] == 2 and joined.last_captured_ns == 1_001


def test_a_block_split_anywhere_is_put_back_together():
    data = block(0) + block(1)
    joined = Reassembler()
    out = []
    for at in range(0, len(data), 7):
        out += joined.feed(data[at : at + 7])
    assert b"".join(out) == b"\x01\x00" * (2 * BLOCK_SAMPLES)


def test_a_repeated_or_late_block_is_dropped():
    joined = Reassembler()
    joined.feed(block(0) + block(2))
    assert joined.feed(block(2)) == [] and joined.feed(block(1)) == []
    assert joined.status()["duplicates"] == 2


def test_a_short_gap_is_filled_with_silence_and_a_long_one_only_counted():
    joined = Reassembler()
    joined.feed(block(0))
    out = joined.feed(block(2))  # one block lost
    assert out[0] == bytes(2 * BLOCK_SAMPLES) and len(out) == 2
    out = joined.feed(block(3, first=3 * BLOCK_SAMPLES + blocks.MAX_FILL + 1))
    assert len(out) == 1
    status = joined.status()
    assert status["gaps"] == 2
    assert status["filled_samples"] == BLOCK_SAMPLES
    assert status["lost_samples"] == BLOCK_SAMPLES + blocks.MAX_FILL + 1


def test_a_gap_the_node_reports_is_counted():
    joined = Reassembler()
    joined.feed(block(0, flags=blocks.FLAG_GAP))
    assert joined.status()["node_gaps"] == 1


@pytest.mark.parametrize(
    "data, message",
    [
        (b"XXXX" + block(0)[4:], "not a block"),
        (block(0)[:6] + b"\x01\x00" + block(0)[8:], "header bits"),
        (block(0, flags=0x80), "header bits"),
        (blocks.HEADER.pack(blocks.MAGIC, 1, 0, 0, 0, 0, 0, 0), "0 samples"),
        (
            blocks.HEADER.pack(blocks.MAGIC, 1, 0, 0, 0, 0, 0, blocks.MAX_BLOCK_SAMPLES + 1),
            "samples",
        ),
    ],
)
def test_what_does_not_parse_is_refused(data, message):
    with pytest.raises(BlockError, match=message):
        Reassembler().feed(data)


def test_a_block_that_overlaps_the_last_is_refused():
    joined = Reassembler()
    joined.feed(block(0))
    with pytest.raises(BlockError, match="overlaps"):
        joined.feed(block(1, first=BLOCK_SAMPLES - 1))


def test_the_published_contract_is_the_format():
    committed = Path(__file__).parents[2] / "contracts" / "satellite" / "v1" / "audio.json"
    assert json.loads(committed.read_text()) == blocks.contract()
    assert blocks.contract()["header_bytes"] == 30


# -- the gateway -------------------------------------------------------------------------


def test_sound_is_carried_from_its_first_byte(gateway, pki):  # noqa: F811
    sink = Sink()
    ticket = gateway.open("zero-entrance", "mic-1", lambda: sink, kind="audio")
    with dial(pki, gateway.port) as connection:
        header = {
            "schema_version": 1,
            "stream_id": ticket.stream_id,
            "source_id": "mic-1",
            "token": ticket.token,
            "kind": "audio",
        }
        connection.sendall(json.dumps(header).encode() + b"\n")
        assert connection.recv(64).startswith(b'{"ok": true}')
        connection.sendall(block(0))
        assert until(lambda: sink.data == block(0))
        assert gateway.status()["streams"][0]["kind"] == "audio"


def test_a_stream_is_only_the_kind_it_was_opened_as(gateway, pki):  # noqa: F811
    ticket = gateway.open("zero-entrance", "mic-1", Sink, kind="audio")
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket, source="mic-1")["ok"] is False  # says video
    assert gateway.refused == {"wrong_kind": 1}
    with pytest.raises(GatewayError):
        gateway.open("zero-entrance", "mic-1", Sink, kind="smell")


# -- the microphone ----------------------------------------------------------------------


class AudioLink:
    def __init__(self) -> None:
        self.started: list[tuple] = []
        self.renewed: list[str] = []
        self.stopped: list[str] = []

    def start_audio(self, node_id, source_id, connect, on_end):
        stream_id = f"sound-{len(self.started)}"
        self.started.append((stream_id, connect, on_end))
        return stream_id

    def renew_audio(self, node_id, stream_id):
        self.renewed.append(stream_id)
        return True

    def stop_audio(self, node_id, stream_id):
        self.stopped.append(stream_id)


@pytest.fixture
def microphone():
    link = AudioLink()
    made = RemoteMicrophone(
        "zero-entrance",
        "mic-1",
        link=link,
        media=MEDIA.model_copy(update={"renew_every_seconds": 1}),
    )
    made.link_for_test = link
    yield made
    made.close()


def test_an_unheard_microphone_asks_for_nothing(microphone):
    time.sleep(0.3)
    assert microphone.link_for_test.started == []
    assert microphone.status()["state"] == "idle"


def test_listening_asks_for_sound_and_hears_it(microphone):
    link = microphone.link_for_test
    with microphone.listen() as heard:
        assert until(lambda: link.started)
        stream_id, connect, _ = link.started[0]
        sink = connect()
        sink.feed(block(0) + block(1))
        assert next(heard) == b"\x01\x00" * (2 * BLOCK_SAMPLES)
        status = microphone.status()
        assert status["state"] == "live" and status["listeners"] == 1
        assert status["blocks"]["blocks"] == 2
        assert status["level_dbfs"] < -80
    assert link.stopped == [stream_id]
    status = microphone.status()
    assert status["state"] == "idle" and status["listeners"] == 0
    assert status["blocks"]["blocks"] == 2  # kept from the connection that ended


def test_only_so_many_browsers_listen_at_once(microphone):
    held = [microphone.subscribe("listening", f"b{n}") for n in range(MAX_LISTENERS)]
    with pytest.raises(BlockingIOError, match="listeners"):
        microphone.subscribe("listening", "one more")
    recording = microphone.subscribe("recording", "rule")  # a recording is not a listener
    for reader in [*held, recording]:
        microphone.unsubscribe(reader)


def test_a_reader_that_falls_behind_loses_its_oldest_sound_only(microphone):
    slow = microphone.subscribe("listening", "slow")
    quick = microphone.subscribe("listening", "quick")
    link = microphone.link_for_test
    assert until(lambda: link.started)
    sink = link.started[0][1]()
    for n in range(40):  # four seconds, twice what a queue keeps
        sink.feed(block(n))
        assert quick.read(0) is not None
    kept = slow.read(0)
    assert len(kept) <= blocks.RATE * 2 * 2
    assert microphone.status()["dropped_bytes"] > 0
    microphone.unsubscribe(slow)
    microphone.unsubscribe(quick)


def test_a_damaged_stream_is_counted_and_ends_its_connection(microphone):
    reader = microphone.subscribe("recording", "rule")
    link = microphone.link_for_test
    assert until(lambda: link.started)
    sink = link.started[0][1]()
    with pytest.raises(BlockError):
        sink.feed(b"XXXX" + bytes(40))
    assert microphone.status()["corrupt_connections"] == 1
    microphone.unsubscribe(reader)


def test_the_table_knows_each_microphone_once(microphone):
    table = MicrophoneTable()
    table.add(microphone)
    with pytest.raises(ValueError):
        table.add(microphone)
    assert MIC in table and table.get(MIC) is microphone
    assert set(table.status()) == {MIC}


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_a_satellite_microphone_is_recorded_like_the_hub_s_own(microphone, tmp_path):
    captures = Captures(tmp_path)
    link = microphone.link_for_test
    stop = threading.Event()

    def speak():
        assert until(lambda: link.started)
        sink = link.started[0][1]()
        for n in range(15):
            sink.feed(block(n, fill=(n * 997) % 3000))
            time.sleep(0.02)
        stop.set()

    speaker = threading.Thread(target=speak)
    speaker.start()
    started = time.monotonic()
    with microphone.recording("rule:test") as sound:
        name = captures.record_audio(sound, 10, "test", stop)
    speaker.join()
    assert time.monotonic() - started < 8
    assert sound.written == 15 * BLOCK_SAMPLES * 2
    assert not sound.pipe.exists()
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(tmp_path / name)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert 1.0 <= float(probe.stdout) <= 2.0
    assert microphone.status()["leases"] == microphone.demands.summary()
    assert not microphone.demands.leases()


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_a_silent_satellite_does_not_hold_a_recording_open(microphone, tmp_path):
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    started = time.monotonic()
    with microphone.recording("rule:test") as sound:
        try:
            Captures(tmp_path).record_audio(sound, 10, "test", stop)
        except HardwareError:
            pass  # nothing was heard, so there may be nothing to keep; either way it ends
    assert time.monotonic() - started < 8
    assert sound.written == 0
    assert not microphone.demands.leases()


# -- the service -------------------------------------------------------------------------


@pytest.fixture
def service(pki, tmp_path):  # noqa: F811
    config = SatellitesConfig(
        enabled=True,
        nodes_file=tmp_path / "nodes.json",
        store_path=tmp_path / "journal.sqlite3",
        mqtt=tls_config(pki).model_dump(),
        media=MEDIA.model_dump(),
    )
    sources = SourceRegistry()
    microphones = MicrophoneTable()
    built = SatelliteService(
        config,
        sources=sources,
        nodes=nodes_for(pki, tmp_path),
        transport=Transport(),
        cameras=SourceManager(sources),
        microphones=microphones,
    )
    built.start()
    built.sessions.broker_connected()
    yield built
    microphones.close()
    built.stop()


MICROPHONE = {"source_id": "mic-1", "kind": "microphone", "options": {"activity": True}}


def test_a_declared_microphone_becomes_one_the_hub_can_hear(service):
    assert service.gateway is not None  # opened for sound alone
    say_hello(service, [MICROPHONE, {"source_id": "mic-2", "kind": "microphone", "enabled": False}])
    assert list(m.source_id for m in service.microphones) == [MIC]
    say_hello(service, [MICROPHONE])
    assert len(list(service.microphones)) == 1


def test_sound_is_asked_for_like_video(service):
    say_hello(service, [MICROPHONE])
    stream_id = service.start_audio("zero-entrance", "mic-1", Sink, Ends())
    _, command = service._transport.commands[-1]
    assert (command["action"], command["source_id"]) == ("audio_start", "mic-1")
    assert len(command["token"]) >= 32
    assert service.gateway.status()["streams"][0]["kind"] == "audio"
    assert service.renew_audio("zero-entrance", stream_id)
    assert service._transport.commands[-1][1]["action"] == "audio_renew"
    service.stop_audio("zero-entrance", stream_id)
    assert service._transport.commands[-1][1]["action"] == "audio_stop"


# -- rules -------------------------------------------------------------------------------


def registry():
    sources = SourceRegistry()
    for source_id, origin in ((MIC, "satellite"), ("legacy-microphone", "local")):
        sources.register(
            SourceRecord(
                ref=SourceRef.parse(source_id),
                kind=SourceKind.MICROPHONE,
                display_name=source_id,
                origin=origin,
                state=SourceState.READY,
            )
        )
    return sources


def sound_rule(actions=None, **trigger):
    return RuleV2(
        id="sound",
        name="Sound",
        trigger=AudioEventTrigger(**{"source_id": MIC, "kind": "audio.activity", **trigger}),
        cooldown_seconds=0,
        actions=actions or [TTSAction(text="Who is there?")],
    )


def test_a_sound_rule_is_armable_and_watches_its_microphone():
    assert "audio_event" in ARMABLE_TRIGGERS
    plan = ResourcePlanner(registry(), satellites_enabled=True).plan([sound_rule()])
    assert plan.ok, plan.problems
    assert plan.watched == {MIC}


def test_a_sound_rule_does_not_take_a_level_or_a_local_microphone():
    planner = ResourcePlanner(registry(), satellites_enabled=True)
    plan = planner.plan([sound_rule(min_level=-20)])
    assert "min_level is not available" in plan.problems[0].message
    plan = planner.plan([sound_rule(source_id="legacy-microphone")])
    assert [p.message for p in plan.problems] == [
        "legacy-microphone reports no sound events; only satellites do"
    ]


def test_a_recording_from_a_satellite_needs_a_microphone_the_hub_can_hear():
    rule = sound_rule([AudioAction(audio_source_id=MIC)])
    assert ResourcePlanner(registry(), satellites_enabled=True, microphones={MIC}).plan([rule]).ok
    plan = ResourcePlanner(registry(), satellites_enabled=True).plan([rule])
    assert "not a microphone this hub can hear" in plan.problems[0].message


def sound(at=None, value=True):
    heard = event(at=at, value=value)
    return dataclasses.replace(heard, ref=SourceRef.parse(MIC), kind="audio.activity")


@pytest.fixture
def listening(engine):  # noqa: F811 - the fixture imported above
    engine.sources.register(
        SourceRecord(
            ref=SourceRef.parse(MIC),
            kind=SourceKind.MICROPHONE,
            display_name="Entrance microphone",
            origin="satellite",
            state=SourceState.READY,
        )
    )
    engine.planner = ResourcePlanner(
        engine.sources, satellites_enabled=True, microphones=engine.microphones
    )
    engine.update_v2(SentryConfigV2(rules=[sound_rule()], test_mode=True), engine.revision)
    engine.arm()
    return engine


def test_sound_on_a_satellite_runs_the_rule(listening):
    listening.observe_event(sound(value=False))
    assert log(listening, "would_run") == []
    listening.observe_event(sound())
    assert log(listening, "would_run") == ["TTS: Who is there?"]


def test_sound_while_the_hub_speaks_or_just_after_is_its_own(listening):
    with listening.audio_lock:
        listening.observe_event(sound())
    listening.quiet_from = time.monotonic()
    listening.observe_event(sound())
    assert log(listening, "would_run") == []
    assert len(log(listening, "skipped")) == 2
    assert "playing its own" in log(listening, "skipped")[0]
    listening.quiet_from = time.monotonic() - ECHO_SECONDS - 0.1
    listening.observe_event(sound())
    assert len(log(listening, "would_run")) == 1

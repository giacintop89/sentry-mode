"""The camera on a satellite: what it runs, when it streams, and that it always stops."""

import json
import socket
import sys
import time
from threading import Thread

import pytest
from test_agent import FakeTransport, build, grant, until

from sentry_satellite import commands, protocol
from sentry_satellite.camera.profile import encoder_argv
from sentry_satellite.camera.publisher import Publisher, Stream
from sentry_satellite.camera.source import CsiCamera
from sentry_satellite.config import ConfigError, check_conflicts, check_options
from sentry_satellite.config import Source as ConfiguredSource

STREAM = Stream(
    stream_id="stream-0001", source_id="camera-1", port=8555, token="t" * 32, hub_epoch=7
)
OPTIONS = check_options("csi", {}, "camera-1")

# A stand-in for rpicam-vid: a keyframe-shaped chunk ten times a second, until killed.
STEADY = """
import sys, time
while True:
    sys.stdout.buffer.write(b"\\0\\0\\0\\1" + b"x" * 60)
    sys.stdout.flush()
    time.sleep(0.1)
"""
SILENT = "import time; time.sleep(60)"
BRIEF = """
import sys
sys.stdout.buffer.write(b"\\0\\0\\0\\1abc")
sys.stderr.write("camera went away\\n")
"""


def encoder(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class Hub:
    """The far end of the media port, one connection at a time, over a socket pair."""

    def __init__(self, answer=None, *, read=True) -> None:
        self.answer = {"ok": True} if answer is None else answer
        self.read = read
        self.headers: list[dict] = []
        self.received: list[bytes] = []
        self.ends: list = []
        self.children = []

    def connect(self, port: int):
        assert port == STREAM.port
        near, far = socket.socketpair()
        self.ends.append(far)
        Thread(target=self._serve, args=(far,), daemon=True).start()
        return near

    def _serve(self, far) -> None:
        received = b""
        with far:
            line = b""
            while not line.endswith(b"\n"):
                chunk = far.recv(1)
                if not chunk:
                    return
                line += chunk
            self.headers.append(json.loads(line))
            far.sendall(json.dumps(self.answer).encode() + b"\n")
            if not self.read:
                time.sleep(30)
                return
            while True:
                try:
                    chunk = far.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                received += chunk
            self.received.append(received)

    def child(self, *arguments, **options):
        from sentry_satellite.subprocesses import Child

        made = Child(*arguments, **options)
        self.children.append(made)
        return made


def publisher(hub: Hub, code: str = STEADY, seconds: float = 30.0, **options) -> Publisher:
    options.setdefault("retry_seconds", 0.05)
    options.setdefault("max_retry_seconds", 0.2)
    return Publisher(
        STREAM,
        encoder(code),
        hub.connect,
        expires_at=time.monotonic() + seconds,
        child=hub.child,
        **options,
    )


def reaped(hub: Hub) -> bool:
    return all(child.poll() is None and child._process is None for child in hub.children)


# -- what the encoder is asked to do ------------------------------------------------------


def test_the_encoder_command_is_built_from_numbers_only():
    argv = encoder_argv({**OPTIONS, "fps": 10, "keyframe_seconds": 2.0}, program="/usr/bin/x")
    assert argv[0] == "/usr/bin/x"
    assert argv[argv.index("--intra") + 1] == "20"
    assert argv[argv.index("--bitrate") + 1] == "1000000"
    assert argv[argv.index("--width") + 1] == "640"
    assert "--inline" in argv and argv[-2:] == ["--output", "-"]


def test_a_camera_defaults_to_the_qualified_profile():
    assert OPTIONS == {
        "enabled": True,
        "profile": "h264-tls",
        "width": 640,
        "height": 480,
        "fps": 10,
        "bitrate_kbps": 1000,
        "keyframe_seconds": 2.0,
    }


@pytest.mark.parametrize(
    "options, problem",
    [
        ({"profile": "mjpeg"}, "profile"),
        ({"fps": 30}, "fps"),
        ({"bitrate_kbps": 50}, "bitrate"),
        ({"width": 800}, "width"),
        ({"command": "rm -rf /"}, "does not take"),
    ],
)
def test_a_camera_outside_the_qualified_profile_is_refused(options, problem):
    with pytest.raises(ConfigError, match=problem):
        check_options("csi", options, "camera-1")


def test_a_size_is_a_pair_that_was_measured():
    odd = ConfiguredSource("camera-1", "csi", check_options("csi", {"height": 720}, "c"))
    with pytest.raises(ConfigError, match="streams at"):
        check_conflicts([odd])


def test_a_board_has_one_camera_port():
    one = ConfiguredSource("camera-1", "csi", OPTIONS)
    two = ConfiguredSource("camera-2", "csi", OPTIONS)
    with pytest.raises(ConfigError, match="one camera port"):
        check_conflicts([one, two])


def test_a_camera_without_its_encoder_says_so_and_does_not_stream(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    camera = CsiCamera("camera-1", OPTIONS)
    assert "not installed" in camera.error
    with pytest.raises(RuntimeError):
        camera.argv()


def test_a_camera_reports_nothing_and_stops_when_asked():
    camera = CsiCamera("camera-1", OPTIONS, program="/bin/true")
    readings = []
    thread = Thread(target=lambda: readings.extend(camera.read()))
    thread.start()
    camera.stop()
    thread.join(1)
    assert not thread.is_alive() and readings == []


# -- the stream ----------------------------------------------------------------------------


def test_a_stream_introduces_itself_and_then_carries_only_video():
    hub = Hub()
    stream = publisher(hub)
    stream.start()
    assert until(lambda: stream.bytes_sent > 200)
    assert stream.state == "live"
    stream.stop()
    assert stream.join(5)
    assert hub.headers == [
        {
            "schema_version": 1,
            "stream_id": "stream-0001",
            "source_id": "camera-1",
            "token": "t" * 32,
        }
    ]
    assert until(lambda: hub.received and hub.received[0].startswith(b"\0\0\0\1"))
    assert stream.state == "stopped"
    assert reaped(hub)


def test_a_refusal_ends_the_stream_without_starting_the_encoder():
    hub = Hub({"ok": False, "detail": "unknown stream"})
    stream = publisher(hub)
    stream.start()
    assert stream.join(5)
    assert stream.state == "refused" and "unknown stream" in stream.error
    assert hub.children == [] and len(hub.headers) == 1


def test_an_encoder_that_exits_is_started_again_on_a_new_connection():
    hub = Hub()
    stream = publisher(hub, BRIEF)
    stream.start()
    assert until(lambda: stream.encoders >= 3)
    stream.stop()
    assert stream.join(5)
    assert stream.connections >= 3
    assert "camera went away" in (stream.last_interruption or "")
    assert reaped(hub)


def test_a_silent_encoder_is_replaced():
    hub = Hub()
    stream = publisher(hub, SILENT, stall_seconds=0.3)
    stream.start()
    assert until(lambda: stream.encoders >= 2)
    stream.stop()
    assert stream.join(5)
    assert "said nothing" in (stream.last_interruption or "")
    assert reaped(hub)


def test_a_hub_that_stops_reading_costs_a_reconnection_not_memory():
    hub = Hub(read=False)
    chatty = "import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)\n"
    stream = publisher(hub, chatty, send_seconds=0.3)
    stream.start()
    assert until(lambda: stream.connections >= 2)
    stream.stop()
    assert stream.join(5)
    assert "not taking the video" in (stream.last_interruption or "")
    assert reaped(hub)


def test_a_hub_that_cannot_be_reached_is_tried_again_until_the_stream_expires():
    attempts = []

    def unreachable(port):
        attempts.append(port)
        raise ConnectionRefusedError("refused")

    stream = Publisher(
        STREAM,
        encoder(STEADY),
        unreachable,
        expires_at=time.monotonic() + 0.5,
        retry_seconds=0.05,
        max_retry_seconds=0.1,
    )
    stream.start()
    assert stream.join(5)
    assert stream.state == "expired" and len(attempts) >= 2 and stream.encoders == 0


def test_a_stream_ends_when_it_runs_out_even_while_video_flows():
    hub = Hub()
    stream = publisher(hub, seconds=0.5)
    stream.start()
    assert stream.join(5)
    assert stream.state == "expired" and stream.bytes_sent > 0
    assert reaped(hub)


def test_a_renewal_moves_the_end_later():
    hub = Hub()
    stream = publisher(hub, seconds=0.5)
    stream.start()
    stream.renew(time.monotonic() + 30)
    time.sleep(1)
    assert stream.alive
    stream.stop()
    assert stream.join(5)


# -- what the hub may say --------------------------------------------------------------


def video(action="video_start", **changes) -> dict:
    message = {
        "command_id": "v-1",
        "action": action,
        "node_id": "zero-entrance",
        "hub_epoch": 7,
        "stream_id": "stream-0001",
        "source_id": "camera-1",
        "port": 8555,
        "token": "t" * 32,
        "duration_seconds": 30,
    }
    message.update(changes)
    return {key: value for key, value in message.items() if value is not None}


def test_a_video_command_names_a_stream_on_the_hub_and_nothing_else():
    command = commands.parse(video(), node_id="zero-entrance")
    assert (command.source_id, command.port, command.stream_id) == ("camera-1", 8555, "stream-0001")


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "evil.example"},
        {"port": 22},
        {"port": True},
        {"token": "short"},
        {"stream_id": "../../x"},
        {"source_id": "Camera 1"},
        {"duration_seconds": 0},
        {"duration_seconds": 3600},
    ],
)
def test_a_video_command_outside_those_bounds_is_refused(changes):
    with pytest.raises(commands.CommandError):
        commands.parse(video(**changes), node_id="zero-entrance")


def test_renewing_or_stopping_a_stream_takes_only_its_id():
    renew = video("video_renew", source_id=None, port=None, token=None)
    assert commands.parse(renew, node_id="zero-entrance").stream_id == "stream-0001"
    stop = video("video_stop", source_id=None, port=None, token=None, duration_seconds=None)
    assert commands.parse(stop, node_id="zero-entrance").action == "video_stop"
    with pytest.raises(commands.CommandError):
        commands.parse(video("video_stop"), node_id="zero-entrance")


def test_other_commands_do_not_describe_streams():
    message = video("grant", capability="events", grant_id="g-1")
    with pytest.raises(commands.CommandError, match="stream"):
        commands.parse(message, node_id="zero-entrance")


# -- the agent ------------------------------------------------------------------------------


class FakePublisher:
    made: list["FakePublisher"] = []

    def __init__(self, stream, argv, connect, *, expires_at, clock) -> None:
        self.stream, self.argv, self.expires_at = stream, argv, expires_at
        self.started = self.stopped = False
        self.renewals = 0
        FakePublisher.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def renew(self, expires_at):
        self.renewals += 1
        self.expires_at = expires_at

    def join(self, timeout):
        return True

    @property
    def alive(self):
        return self.started and not self.stopped

    def status(self):
        return {"stream_id": self.stream.stream_id, "state": "live"}


def camera_agent(**options):
    FakePublisher.made = []
    camera = CsiCamera("camera-1", OPTIONS, program="/usr/bin/rpicam-vid")
    agent, transport = build([camera])
    agent.connect_media = options.get("connect", lambda port: None)
    agent.publisher = FakePublisher
    agent.start()
    return agent, transport


def ask(transport, **message):
    transport.deliver(protocol.topic("zero-entrance", "commands"), video(**message))


def last_ack(transport, command_id):
    acks = [ack for ack in transport.on("acks") if ack["command_id"] == command_id]
    return acks[-1] if acks else None


def test_no_video_without_a_grant():
    agent, transport = camera_agent()
    try:
        ask(transport)
        assert until(lambda: last_ack(transport, "v-1"))
        assert last_ack(transport, "v-1")["outcome"] == "failed"
        assert FakePublisher.made == []
    finally:
        agent.stop()


def test_no_video_for_another_run_of_the_hub():
    agent, transport = camera_agent()
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport, hub_epoch=6)
        assert until(lambda: last_ack(transport, "v-1"))
        assert "another run" in last_ack(transport, "v-1")["detail"]
    finally:
        agent.stop()


def test_a_granted_camera_streams_until_the_grant_is_taken_back():
    agent, transport = camera_agent()
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport)
        assert until(lambda: last_ack(transport, "v-1"))
        assert last_ack(transport, "v-1")["outcome"] == "applied"
        [stream] = FakePublisher.made
        assert stream.started and stream.argv[0] == "/usr/bin/rpicam-vid"
        assert stream.stream.port == 8555

        ask(
            transport, command_id="v-2", action="video_renew", source_id=None, port=None, token=None
        )
        assert until(lambda: stream.renewals == 1)

        assert until(
            lambda: any("video" in h["sources"]["camera-1"] for h in transport.on("health"))
        )
        grant(transport, agent, command_id="c-2", action="revoke")
        assert until(lambda: stream.stopped)
        assert agent._videos == {}
    finally:
        agent.stop()


def test_the_hub_can_stop_a_stream_and_stopping_it_twice_is_harmless():
    agent, transport = camera_agent()
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport)
        assert until(lambda: FakePublisher.made)
        stop = dict(source_id=None, port=None, token=None, duration_seconds=None)
        ask(transport, command_id="v-2", action="video_stop", **stop)
        assert until(lambda: FakePublisher.made[0].stopped)
        ask(transport, command_id="v-3", action="video_stop", **stop)
        assert until(lambda: last_ack(transport, "v-3"))
        assert last_ack(transport, "v-3")["outcome"] == "applied"
    finally:
        agent.stop()


def test_a_second_start_for_the_same_stream_is_a_renewal():
    agent, transport = camera_agent()
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport)
        ask(transport, command_id="v-2")
        assert until(lambda: last_ack(transport, "v-2"))
        assert len(FakePublisher.made) == 1 and FakePublisher.made[0].renewals == 1
    finally:
        agent.stop()


def test_only_a_camera_can_stream():
    agent, transport = camera_agent()
    try:
        grant(transport, agent)
        assert until(lambda: agent._grant is not None)
        ask(transport, source_id="pir-1")
        assert until(lambda: last_ack(transport, "v-1"))
        assert "not a camera" in last_ack(transport, "v-1")["detail"]
    finally:
        agent.stop()


def test_stopping_the_agent_stops_its_streams():
    agent, transport = camera_agent()
    grant(transport, agent)
    assert until(lambda: agent._grant is not None)
    ask(transport)
    assert until(lambda: FakePublisher.made)
    agent.stop()
    assert FakePublisher.made[0].stopped and agent.stopped_cleanly


def test_a_real_stream_through_the_agent_is_reaped_on_stop():
    hub = Hub()
    FakePublisher.made = []
    camera = CsiCamera("camera-1", OPTIONS, program=sys.executable)
    camera.argv = lambda: encoder(STEADY)
    agent, transport = build([camera], transport=FakeTransport())
    agent.connect_media = hub.connect
    agent.publisher = lambda *a, **k: Publisher(*a, child=hub.child, **k)
    agent.start()
    grant(transport, agent)
    assert until(lambda: agent._grant is not None)
    ask(transport)
    assert until(lambda: hub.received or (agent._videos and agent._videos["camera-1"].bytes_sent))
    agent.stop()
    assert agent.stopped_cleanly
    assert reaped(hub)

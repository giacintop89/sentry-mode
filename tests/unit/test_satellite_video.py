"""Video from satellites: the door it comes in through, the decoder, and the camera around them.

The gateway is tested over real mutual TLS, with certificates made for the test by
openssl; the decoder with the real ffmpeg where there is one, and with stand-ins that
refuse to read or refuse to die where the point is how it copes.
"""

import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

import pytest

from sentry_mode.config import DetectionConfig
from sentry_mode.satellites.config import MediaConfig, SatellitesConfig
from sentry_mode.satellites.identity import NodeRegistry
from sentry_mode.satellites.service import SatelliteService
from sentry_mode.sources.manager import SourceManager
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.media_gateway import GatewayError, MediaGateway, server_context
from sentry_mode.vision.network import Backlog, DecoderGone, NetworkDecoder, keyframe_offset
from sentry_mode.vision.remote import RemoteCamera

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

FFMPEG = shutil.which("ffmpeg")
SPS = b"\x00\x00\x00\x01\x67"


def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def openssl(*arguments: str) -> None:
    subprocess.run(["openssl", *arguments], check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> Path:
    """A satellites CA, a hub certificate for 127.0.0.1, and two node certificates."""
    where = tmp_path_factory.mktemp("pki")
    curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    openssl(
        "req", "-x509", *curve, "-keyout", str(where / "ca.key"), "-out", str(where / "ca.crt"),
        "-days", "2", "-subj", "/CN=test CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign",
    )  # fmt: skip
    leaves = {
        "hub": "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\n",
        "zero-entrance": "subjectAltName=DNS:zero-entrance\nextendedKeyUsage=clientAuth\n",
        "zero-garage": "subjectAltName=DNS:zero-garage\nextendedKeyUsage=clientAuth\n",
    }
    for name, extensions in leaves.items():
        (where / f"{name}.ext").write_text(extensions)
        openssl(
            "req", *curve, "-keyout", str(where / f"{name}.key"),
            "-out", str(where / f"{name}.csr"), "-subj", f"/CN={name}",
        )  # fmt: skip
        openssl(
            "x509", "-req", "-in", str(where / f"{name}.csr"), "-CA", str(where / "ca.crt"),
            "-CAkey", str(where / "ca.key"), "-CAcreateserial", "-days", "2",
            "-out", str(where / f"{name}.crt"), "-extfile", str(where / f"{name}.ext"),
        )  # fmt: skip
    return where


@pytest.fixture(scope="module")
def h264(tmp_path_factory) -> bytes:
    """Two seconds of 320x240 H.264, parameter sets repeated before every keyframe."""
    if FFMPEG is None:
        pytest.skip("needs ffmpeg")
    out = tmp_path_factory.mktemp("video") / "clip.h264"
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=size=320x240:rate=10", "-t", "2", "-c:v", "libx264",
            "-g", "5", "-bf", "0", "-bsf:v", "dump_extra", "-pix_fmt", "yuv420p",
            "-f", "h264", str(out),
        ],
        check=True,
    )  # fmt: skip
    return out.read_bytes()


def nodes_for(pki: Path, tmp_path: Path) -> NodeRegistry:
    nodes = NodeRegistry(tmp_path / "nodes.json")
    for name in ("zero-entrance", "zero-garage"):
        certificate = (pki / f"{name}.crt").read_text()
        nodes.register(name, certificate=certificate)
        nodes.approve(name, certificate=certificate)
    return nodes


def tls_config(pki: Path):
    return SatellitesConfig(
        mqtt={
            "tls_ca_file": pki / "ca.crt",
            "tls_cert_file": pki / "hub.crt",
            "tls_key_file": pki / "hub.key",
        }
    ).mqtt


MEDIA = MediaConfig(
    bind_host="127.0.0.1",
    port=0,
    stream_seconds=30,
    renew_every_seconds=5,
    stall_seconds=1,
    handshake_seconds=2,
    connect_timeout_seconds=2,
    max_streams=2,
)


class Sink:
    def __init__(self, *, refuse_after: int | None = None) -> None:
        self.data = b""
        self.closed = threading.Event()
        self.refuse_after = refuse_after

    def feed(self, data: bytes) -> None:
        if self.refuse_after is not None and len(self.data) >= self.refuse_after:
            raise Backlog("behind")
        self.data += data

    def close(self) -> None:
        self.closed.set()


class Ends:
    def __init__(self) -> None:
        self.seen: list[tuple[str, bool]] = []

    def __call__(self, reason: str, over: bool) -> None:
        self.seen.append((reason, over))


@pytest.fixture
def gateway(pki, tmp_path):
    made = MediaGateway(MEDIA, server_context(tls_config(pki)), nodes_for(pki, tmp_path))
    made.start()
    yield made
    made.stop()


def dial(pki: Path, port: int, node: str = "zero-entrance") -> ssl.SSLSocket:
    context = ssl.create_default_context(cafile=str(pki / "ca.crt"))
    context.load_cert_chain(str(pki / f"{node}.crt"), str(pki / f"{node}.key"))
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    return context.wrap_socket(raw, server_hostname="127.0.0.1")


def introduce(connection, ticket, *, source="camera-1", token=None, stream_id=None) -> dict:
    header = {
        "schema_version": 1,
        "stream_id": stream_id or ticket.stream_id,
        "source_id": source,
        "token": token or ticket.token,
    }
    connection.sendall(json.dumps(header).encode() + b"\n")
    reply = b""
    while not reply.endswith(b"\n"):
        chunk = connection.recv(1)
        if not chunk:
            break
        reply += chunk
    return json.loads(reply) if reply else {}


# -- finding where to start --------------------------------------------------------------


def test_decoding_starts_at_the_first_parameter_set():
    assert keyframe_offset(b"\x00\x00\x00\x01\x41junk" + SPS + b"rest") == 9
    assert keyframe_offset(b"xx\x00\x00\x01\x67") == 2
    assert keyframe_offset(b"\x00\x00\x00\x01\x41only a slice") is None
    assert keyframe_offset(b"\x00\x00\x01") is None


# -- the gateway -------------------------------------------------------------------------


def test_a_stream_the_hub_opened_is_carried_from_its_first_keyframe(gateway, pki):
    sink, ends = Sink(), Ends()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket) == {"ok": True}
        connection.sendall(b"\x00\x00\x00\x01\x41late slice" + SPS + b"keyframe")
        assert until(lambda: sink.data == SPS + b"keyframe")
        assert gateway.status()["streams"][0]["connected"]
    assert until(lambda: sink.closed.is_set())
    assert until(lambda: ends.seen == [("the node closed the connection", False)])


def test_another_node_cannot_take_a_stream_even_with_its_token(gateway, pki):
    sink = Sink()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink)
    with dial(pki, gateway.port, node="zero-garage") as connection:
        assert introduce(connection, ticket) == {"ok": False, "detail": "unknown stream"}
    assert gateway.refused == {"unknown_stream": 1}


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"token": "x" * 43}, "wrong_token"),
        ({"source": "camera-2"}, "wrong_source"),
        ({"stream_id": "0" * 16}, "unknown_stream"),
    ],
)
def test_a_stream_is_only_what_it_was_opened_as(gateway, pki, changes, reason):
    ticket = gateway.open("zero-entrance", "camera-1", Sink)
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket, **changes)["ok"] is False
    assert gateway.refused == {reason: 1}


def test_a_revoked_node_is_refused_at_the_door(gateway, pki):
    ticket = gateway.open("zero-entrance", "camera-1", Sink)
    gateway.nodes.revoke("zero-entrance")
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket)["detail"] == "this node is not trusted"


def test_a_certificate_from_elsewhere_does_not_get_past_tls(gateway, pki, tmp_path):
    openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
        "-keyout", str(tmp_path / "x.key"), "-out", str(tmp_path / "x.crt"),
        "-days", "1", "-subj", "/CN=zero-entrance",
    )  # fmt: skip
    context = ssl.create_default_context(cafile=str(pki / "ca.crt"))
    context.load_cert_chain(str(tmp_path / "x.crt"), str(tmp_path / "x.key"))
    with socket.create_connection(("127.0.0.1", gateway.port), timeout=5) as raw:
        with pytest.raises((ssl.SSLError, ConnectionError)):
            with context.wrap_socket(raw, server_hostname="127.0.0.1") as connection:
                connection.sendall(b"{}\n")
                connection.recv(1)
    assert until(lambda: gateway.refused.get("tls") == 1)


def test_a_connection_that_sends_nothing_is_dropped_and_may_come_back(gateway, pki):
    sink, ends = Sink(), Ends()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket)["ok"]
        assert until(lambda: ends.seen, timeout=3)
    assert ends.seen == [("no video for 1 s", False)]
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket)["ok"]


def test_a_stream_has_one_publisher_at_a_time(gateway, pki):
    ticket = gateway.open("zero-entrance", "camera-1", Sink)
    with dial(pki, gateway.port) as first, dial(pki, gateway.port) as second:
        assert introduce(first, ticket)["ok"]
        assert introduce(second, ticket)["detail"] == "already connected"


def test_ending_a_node_closes_a_publication_already_open(gateway, pki):
    sink, ends = Sink(), Ends()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket)["ok"]
        connection.sendall(SPS + b"x")
        assert until(lambda: sink.data)
        assert gateway.close_node("zero-entrance", "the node was revoked") == 1
        assert until(lambda: ends.seen == [("the node was revoked", True)])
        connection.settimeout(2)
        assert connection.recv(10) == b""
    assert gateway.status()["streams"] == []


def test_a_stream_that_is_not_renewed_is_cut_off(pki, tmp_path):
    clock = [0.0]
    gateway = MediaGateway(
        MEDIA, server_context(tls_config(pki)), nodes_for(pki, tmp_path), clock=lambda: clock[0]
    )
    gateway.start()
    try:
        sink, ends = Sink(), Ends()
        ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
        with dial(pki, gateway.port) as connection:
            assert introduce(connection, ticket)["ok"]
            connection.sendall(SPS)
            assert until(lambda: sink.data)
            assert gateway.renew(ticket.stream_id)
            clock[0] = 31
            assert until(lambda: ends.seen == [("the stream was not renewed", True)])
        assert not gateway.renew(ticket.stream_id)
    finally:
        gateway.stop()


def test_a_sink_that_falls_behind_ends_the_connection_not_the_gateway(gateway, pki):
    sink, ends = Sink(refuse_after=1), Ends()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
    with dial(pki, gateway.port) as connection:
        assert introduce(connection, ticket)["ok"]
        connection.sendall(SPS)
        assert until(lambda: sink.data)
        connection.sendall(b"more")
        assert until(lambda: ends.seen == [("behind", False)])
    assert gateway.status()["listening"]


def test_a_restarted_gateway_does_not_know_old_streams(pki, tmp_path):
    first = MediaGateway(MEDIA, server_context(tls_config(pki)), nodes_for(pki, tmp_path))
    first.start()
    ticket = first.open("zero-entrance", "camera-1", Sink)
    first.stop()
    with pytest.raises(GatewayError):
        first.open("zero-entrance", "camera-1", Sink)
    second = MediaGateway(MEDIA, server_context(tls_config(pki)), first.nodes)
    second.start()
    try:
        with dial(pki, second.port) as connection:
            assert introduce(connection, ticket)["detail"] == "unknown stream"
    finally:
        second.stop()


def test_the_gateway_takes_a_bounded_number_of_streams(gateway):
    gateway.open("zero-entrance", "camera-1", Sink)
    gateway.open("zero-garage", "camera-1", Sink)
    with pytest.raises(GatewayError, match="most allowed"):
        gateway.open("zero-garage", "camera-2", Sink)


def test_stopping_the_gateway_ends_what_it_carries(pki, tmp_path):
    gateway = MediaGateway(MEDIA, server_context(tls_config(pki)), nodes_for(pki, tmp_path))
    gateway.start()
    sink, ends = Sink(), Ends()
    ticket = gateway.open("zero-entrance", "camera-1", lambda: sink, on_end=ends)
    connection = dial(pki, gateway.port)
    assert introduce(connection, ticket)["ok"]
    started = time.monotonic()
    gateway.stop()
    assert time.monotonic() - started < 3
    assert ends.seen == [("the hub is stopping", True)]
    connection.close()


# -- the decoder -----------------------------------------------------------------------


def stand_in(tmp_path: Path, body: str) -> str:
    """An 'ffmpeg' that is a shell script, for the ways a decoder can misbehave."""
    path = tmp_path / "ffmpeg"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def reaped(decoder: NetworkDecoder) -> bool:
    process = decoder._process
    return process is not None and process.returncode is not None


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_a_real_stream_is_decoded_to_frames_of_the_expected_size(h264):
    frames = []
    decoder = NetworkDecoder(320, 240, frames.append, ffmpeg=FFMPEG)
    decoder.start()
    for start in range(0, len(h264), 4096):
        decoder.feed(h264[start : start + 4096])
    assert until(lambda: len(frames) >= 10)
    decoder.close()
    assert frames[0].shape == (240, 320, 3)
    assert reaped(decoder)


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_a_stream_is_scaled_to_the_size_the_hub_expects(h264):
    frames = []
    decoder = NetworkDecoder(640, 480, frames.append, ffmpeg=FFMPEG)
    decoder.start()
    decoder.feed(h264)
    assert until(lambda: frames)
    decoder.close()
    assert frames[0].shape == (480, 640, 3)


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_a_truncated_or_damaged_stream_costs_the_decoder_nothing_worse(h264):
    frames = []
    decoder = NetworkDecoder(320, 240, frames.append, ffmpeg=FFMPEG)
    decoder.start()
    decoder.feed(h264[: len(h264) // 3])
    decoder.feed(bytes(range(256)) * 64)
    decoder.feed(h264[len(h264) // 2 :])
    assert until(lambda: len(frames) >= 5)
    started = time.monotonic()
    decoder.close()
    assert time.monotonic() - started < 3
    assert reaped(decoder)


def test_a_decoder_that_will_not_read_is_refused_more_rather_than_buffered(tmp_path):
    decoder = NetworkDecoder(
        320, 240, lambda frame: None, ffmpeg=stand_in(tmp_path, "exec sleep 60"),
        backlog_bytes=256 * 1024,
    )  # fmt: skip
    decoder.start()
    with pytest.raises(Backlog):
        for _ in range(1000):
            decoder.feed(b"x" * 16384)
    assert decoder.queued_bytes <= 256 * 1024
    decoder.close()
    assert reaped(decoder)


def test_stalled_remote_decoder_is_killed_without_ui_block(tmp_path):
    stubborn = stand_in(tmp_path, "trap '' TERM\nsleep 60")
    decoder = NetworkDecoder(320, 240, lambda frame: None, ffmpeg=stubborn, grace_seconds=0.5)
    decoder.start()
    time.sleep(0.2)
    started = time.monotonic()
    decoder.close()
    assert time.monotonic() - started < 3
    assert reaped(decoder)


def test_a_decoder_that_dies_says_so_and_takes_nothing_more(tmp_path):
    decoder = NetworkDecoder(
        320, 240, lambda frame: None, ffmpeg=stand_in(tmp_path, "echo broken >&2; exit 3")
    )
    decoder.start()
    assert until(lambda: not decoder.alive)
    with pytest.raises(DecoderGone):
        decoder.feed(b"x")
    assert until(lambda: "broken" in (decoder.error or ""))
    decoder.close()
    assert reaped(decoder)


def test_a_decoder_that_cannot_start_is_reported(tmp_path):
    decoder = NetworkDecoder(320, 240, lambda frame: None, ffmpeg=str(tmp_path / "missing"))
    with pytest.raises(DecoderGone, match="could not start"):
        decoder.start()
    assert decoder.close() is None


# -- the camera --------------------------------------------------------------------------


class FakeDecoder:
    def __init__(self, width, height, on_frame, **options) -> None:
        self.on_frame, self.size = on_frame, (width, height)
        self.frames = self.bytes_in = self.queued_bytes = 0
        self.error = None
        self.closed = False

    def start(self) -> None:
        pass

    def feed(self, data: bytes) -> None:
        import numpy as np

        self.frames += 1
        self.on_frame(np.zeros((self.size[1], self.size[0], 3), np.uint8))

    def close(self) -> None:
        self.closed = True


class FakeLink:
    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.started: list[tuple] = []
        self.renewed: list[str] = []
        self.stopped: list[str] = []
        self.renew_ok = True

    def start_video(self, node_id, source_id, connect, on_end):
        if self.refuse:
            raise BlockingIOError(self.refuse)
        stream_id = f"stream-{len(self.started)}"
        self.started.append((stream_id, connect, on_end))
        return stream_id

    def renew_video(self, node_id, stream_id):
        self.renewed.append(stream_id)
        return self.renew_ok

    def stop_video(self, node_id, stream_id):
        self.stopped.append(stream_id)


@pytest.fixture
def scheduler():
    from sentry_mode.vision.scheduler import InferenceScheduler

    made = InferenceScheduler(DetectionConfig())
    yield made
    made.close()


def remote(link, scheduler, **media) -> RemoteCamera:
    settings = MEDIA.model_copy(update={"renew_every_seconds": 1, **media})
    return RemoteCamera(
        "zero-entrance",
        "camera-1",
        {"width": 320, "height": 240, "fps": 10},
        link=link,
        scheduler=scheduler,
        detection=DetectionConfig(),
        media=settings,
        decoder=FakeDecoder,
    )


def test_an_unleased_remote_camera_asks_for_nothing(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler)
    time.sleep(0.3)
    assert link.started == [] and camera.status()["state"] == "idle"
    camera.close()


def test_a_node_offline_at_start_is_reported_and_tried_again(scheduler):
    link = FakeLink(refuse="zero-entrance is offline")
    camera = remote(link, scheduler)
    lease = camera.hold("preview", "browser")
    try:
        assert until(lambda: camera.status()["state"] == "offline")
        status = camera.status()
        assert status["error"] == "zero-entrance is offline"
        assert status["capture_running"] is False
        link.refuse = None
        assert until(lambda: link.started, timeout=4)
    finally:
        lease.release()
    camera.close()


def test_video_that_arrives_makes_the_camera_live_and_releasing_it_stops_the_stream(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler)
    lease = camera.hold("preview", "browser")
    assert until(lambda: link.started)
    stream_id, connect, on_end = link.started[0]
    decoder = connect()
    decoder.feed(b"frame")
    status = camera.status()
    assert status["state"] == "live" and status["capture_running"]
    assert camera.latest_frame().shape == (240, 320, 3)
    assert until(lambda: link.renewed, timeout=3)
    lease.release()
    assert link.stopped == [stream_id]
    assert decoder.closed
    assert camera.status()["state"] == "idle" and camera.latest_frame() is None
    camera.close()


def test_a_node_that_never_connects_is_given_up_on_and_asked_again(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler, connect_timeout_seconds=2)
    lease = camera.hold("preview", "browser")
    try:
        assert until(lambda: len(link.started) >= 2, timeout=6)
        assert link.stopped[0] == "stream-0"
        assert camera.status()["failures"] >= 1
    finally:
        lease.release()
    camera.close()


def test_a_connection_without_frames_is_stale_not_offline(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler, connect_timeout_seconds=2)
    lease = camera.hold("preview", "browser")
    try:
        assert until(lambda: link.started)
        link.started[0][1]()
        time.sleep(2.5)
        assert camera.status()["state"] == "stale"
        assert camera.status()["capture_running"]
        assert len(link.started) == 1
    finally:
        lease.release()
    camera.close()


def test_a_stream_that_ends_is_replaced_while_someone_watches(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler)
    lease = camera.hold("preview", "browser")
    try:
        assert until(lambda: link.started)
        _, _, on_end = link.started[0]
        on_end("the node went offline", True)
        assert until(lambda: len(link.started) == 2)
        assert camera.status()["last_end"] == "the node went offline"
    finally:
        lease.release()
    camera.close()


def test_a_renewal_the_hub_cannot_make_replaces_the_stream(scheduler):
    link = FakeLink()
    link.renew_ok = False
    camera = remote(link, scheduler)
    lease = camera.hold("preview", "browser")
    try:
        assert until(lambda: len(link.started) >= 2, timeout=4)
        assert link.stopped[0] == "stream-0"
    finally:
        lease.release()
    camera.close()


def test_a_connection_for_a_stream_no_longer_wanted_is_refused(scheduler):
    link = FakeLink()
    camera = remote(link, scheduler)
    lease = camera.hold("preview", "browser")
    assert until(lambda: link.started)
    _, connect, on_end = link.started[0]
    lease.release()
    with pytest.raises(RuntimeError, match="no longer wanted"):
        connect()
    on_end("late news", False)
    assert camera.status()["last_end"] is None
    camera.close()


# -- the service -----------------------------------------------------------------------


class Transport:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def publish_command(self, node_id: str, payload: bytes) -> bool:
        self.commands.append((node_id, json.loads(payload)))
        return True

    def clear_retained_state(self, node_id: str) -> bool:
        return True

    def settle(self, message) -> bool:
        return True


@pytest.fixture
def service(pki, tmp_path):
    from sentry_mode.vision.scheduler import InferenceScheduler

    config = SatellitesConfig(
        enabled=True,
        nodes_file=tmp_path / "nodes.json",
        store_path=tmp_path / "journal.sqlite3",
        mqtt=tls_config(pki).model_dump(),
        media=MEDIA.model_dump(),
    )
    sources = SourceRegistry()
    cameras = SourceManager(sources)
    inference = InferenceScheduler(DetectionConfig())
    built = SatelliteService(
        config,
        sources=sources,
        nodes=nodes_for(pki, tmp_path),
        transport=Transport(),
        cameras=cameras,
        inference=inference,
        detection=DetectionConfig(),
    )
    built.start()
    built.sessions.broker_connected()
    yield built
    cameras.close()
    built.stop()
    inference.close()


def say_hello(service, sources) -> None:
    from sentry_mode.satellites.mqtt import Message

    document = {
        "schema_version": 1,
        "node_id": "zero-entrance",
        "boot_id": "6f1c2d3e-4a5b-4c7d-8e9f-0a1b2c3d4e5f",
        "connection_id": "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21",
        "online": True,
        "profile": "camera-sensor",
        "sources": sources,
    }
    service.handle(
        Message(
            node_id="zero-entrance",
            channel="state",
            payload=json.dumps(document).encode(),
            retained=False,
            topic="sentry/v1/nodes/zero-entrance/state",
        )
    )


CAMERA = {"source_id": "camera-1", "kind": "csi", "options": {"width": 320, "height": 240}}


def test_a_declared_satellite_camera_becomes_a_camera_of_the_hub(service):
    say_hello(service, [CAMERA, {"source_id": "pir-1", "kind": "gpio"}])
    camera = service.cameras.camera("zero-entrance.camera-1")
    assert camera is not None and (camera.width, camera.height) == (320, 240)
    assert "zero-entrance.pir-1" not in service.cameras
    say_hello(service, [CAMERA])
    assert service.cameras.camera("zero-entrance.camera-1") is camera
    assert service.status()["media"]["gateway"]["listening"]


def test_video_is_asked_for_with_a_token_the_hub_keeps(service):
    say_hello(service, [CAMERA])
    stream_id = service.start_video("zero-entrance", "camera-1", Sink, Ends())
    node, command = service._transport.commands[-1]
    assert (node, command["action"], command["stream_id"]) == (
        "zero-entrance",
        "video_start",
        stream_id,
    )
    assert command["port"] == service.gateway.port and len(command["token"]) >= 32
    assert command["hub_epoch"] == service.sessions.current("zero-entrance").hub_epoch
    assert "token" not in json.dumps(service.status())
    assert service.renew_video("zero-entrance", stream_id)
    assert service._transport.commands[-1][1]["action"] == "video_renew"
    service.stop_video("zero-entrance", stream_id)
    assert service._transport.commands[-1][1]["action"] == "video_stop"
    assert not service.renew_video("zero-entrance", stream_id)


def test_no_video_from_a_node_that_is_not_there(service):
    with pytest.raises(BlockingIOError, match="offline"):
        service.start_video("zero-entrance", "camera-1", Sink, Ends())


def test_revoking_a_node_ends_its_open_video(service, pki):
    say_hello(service, [CAMERA])
    sink, ends = Sink(), Ends()
    service.start_video("zero-entrance", "camera-1", lambda: sink, ends)
    ticket_command = service._transport.commands[-1][1]
    with dial(pki, service.gateway.port) as connection:
        header = {
            "schema_version": 1,
            "stream_id": ticket_command["stream_id"],
            "source_id": "camera-1",
            "token": ticket_command["token"],
        }
        connection.sendall(json.dumps(header).encode() + b"\n")
        assert connection.recv(64).startswith(b'{"ok": true}')
        connection.sendall(SPS)
        assert until(lambda: sink.data)
        service.revoke("zero-entrance", reason="test")
        assert until(lambda: ends.seen == [("the node was revoked", True)])


def test_a_node_going_offline_ends_its_video(service):
    from sentry_mode.satellites.mqtt import Message

    say_hello(service, [CAMERA])
    ends = Ends()
    service.start_video("zero-entrance", "camera-1", Sink, ends)
    goodbye = {
        "schema_version": 1,
        "node_id": "zero-entrance",
        "connection_id": "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21",
        "online": False,
    }
    service.handle(
        Message(
            node_id="zero-entrance",
            channel="state",
            payload=json.dumps(goodbye).encode(),
            retained=False,
            topic="sentry/v1/nodes/zero-entrance/state",
        )
    )
    assert ends.seen == [("the node went offline", True)]
    with pytest.raises(BlockingIOError):
        service.start_video("zero-entrance", "camera-1", Sink, Ends())


def test_a_hub_whose_media_port_cannot_open_keeps_its_events(pki, tmp_path):
    taken = socket.create_server(("127.0.0.1", 0))
    try:
        config = SatellitesConfig(
            enabled=True,
            nodes_file=tmp_path / "nodes.json",
            store_path=tmp_path / "journal.sqlite3",
            mqtt=tls_config(pki).model_dump(),
            media={**MEDIA.model_dump(), "port": taken.getsockname()[1]},
        )
        sources = SourceRegistry()
        built = SatelliteService(
            config,
            sources=sources,
            nodes=nodes_for(pki, tmp_path),
            transport=Transport(),
            cameras=SourceManager(sources),
            inference=object(),
            detection=DetectionConfig(),
        )
        built.start()
        try:
            assert built.gateway is None
            assert built.status()["media"]["error"]
            with pytest.raises(BlockingIOError):
                built.start_video("zero-entrance", "camera-1", Sink, Ends())
        finally:
            built.stop()
    finally:
        taken.close()


def test_media_settings_renew_well_before_expiry():
    with pytest.raises(ValueError, match="renewed"):
        MediaConfig(stream_seconds=30, renew_every_seconds=20)
    with pytest.raises(ValueError, match="1024"):
        MediaConfig(port=554)

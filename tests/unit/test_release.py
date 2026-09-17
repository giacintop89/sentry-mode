"""What a release has to hold: revocation everywhere, one wire version, and a way back.

These are the acceptance checks that cross a boundary rather than live inside one module:
taking a node away has to reach all four channels at once, an agent from another version
must not look healthy, and the administrative listener must not be reachable from the
network the satellites are on. The release checklist names which of these stands for which
ACC ([the checklist](../../docs/release-checklist.md)).
"""

import json
import sys
from pathlib import Path

import pytest

from sentry_mode import security
from sentry_mode.satellites.protocol import SCHEMA_VERSION
from sentry_mode.security import addresses, exposure

sys.path.insert(0, str(Path(__file__).parent))  # the media fixtures live beside this file

from test_satellite_video import (  # noqa: E402, F401 - fixtures, shared with the media tests
    CAMERA,
    MEDIA,
    Ends,
    Sink,
    Transport,
    dial,
    nodes_for,
    pki,
    say_hello,
    service,
    tls_config,
    until,
)

MICROPHONE = {"source_id": "mic-1", "kind": "microphone", "options": {"activity": True}}
SPS = bytes([0, 0, 0, 1, 0x67, 0x42, 0x00, 0x1E])


def publish(service, pki, source_id, kind):  # noqa: F811
    """Open one media stream the way a node does, and hand back what it is writing to."""
    sink, ends = Sink(), Ends()
    start = service.start_video if kind == "video" else service.start_audio
    start("zero-entrance", source_id, lambda: sink, ends)
    command = service._transport.commands[-1][1]
    connection = dial(pki, service.gateway.port)
    header = {
        "schema_version": 1,
        "stream_id": command["stream_id"],
        "source_id": source_id,
        "token": command["token"],
        "kind": kind,
    }
    connection.sendall(json.dumps(header).encode() + b"\n")
    assert connection.recv(64).startswith(b'{"ok": true}')
    connection.sendall(SPS)
    assert until(lambda: sink.data)
    return connection, ends, command


def test_revoking_a_node_takes_back_all_four_channels_at_once(service, pki):  # noqa: F811
    say_hello(service, [CAMERA, MICROPHONE, {"source_id": "pir-1", "kind": "gpio"}])
    video, video_ends, video_command = publish(service, pki, "camera-1", "video")
    audio, audio_ends, audio_command = publish(service, pki, "mic-1", "audio")
    try:
        service.revoke("zero-entrance", reason="sold")

        # Events: the session and its grant are gone, so nothing it sends is acted on.
        assert service.sessions.current("zero-entrance") is None
        assert service.sources.require("zero-entrance.pir-1").state.value == "disabled"

        # Commands: the hub will not send it anything else, and says why.
        with pytest.raises(BlockingIOError, match="not approved"):
            service.configure("zero-entrance", [{"id": "pir-1", "kind": "gpio"}])

        # Video and audio: both publications end, and neither ticket can be used again.
        assert until(lambda: video_ends.seen == [("the node was revoked", True)])
        assert until(lambda: audio_ends.seen == [("the node was revoked", True)])
        for command, source_id, kind in (
            (video_command, "camera-1", "video"),
            (audio_command, "mic-1", "audio"),
        ):
            with dial(pki, service.gateway.port) as again:
                header = {
                    "schema_version": 1,
                    "stream_id": command["stream_id"],
                    "source_id": source_id,
                    "token": command["token"],
                    "kind": kind,
                }
                again.sendall(json.dumps(header).encode() + b"\n")
                assert b'"ok": true' not in again.recv(64)
    finally:
        video.close()
        audio.close()


def test_the_node_is_told_once_and_the_snapshot_it_left_behind_is_erased(service, pki):  # noqa: F811
    say_hello(service, [CAMERA])
    service.revoke("zero-entrance", reason="sold")
    assert service._transport.commands[-1][1]["action"] == "revoke"
    assert service.health.status()["nodes"] == {}
    assert service.nodes.get("zero-entrance").status == "revoked"


# -- one wire version --------------------------------------------------------------------


def test_the_hub_says_which_wire_it_speaks_and_the_contract_agrees():
    schema = Path(__file__).resolve().parents[2] / "contracts/satellite/v1/event.schema.json"
    contract = json.loads(schema.read_text())
    assert contract["properties"]["schema_version"]["const"] == SCHEMA_VERSION


# -- what the network may reach ----------------------------------------------------------


def test_an_administrative_listener_on_the_satellite_network_is_a_finding():
    found = exposure(
        listeners=[("dashboard", "0.0.0.0", 8083), ("media gateway", "0.0.0.0", 8084)],
        satellite_network="192.168.11.0/24",
        addresses=["192.168.1.10", "192.168.11.10"],
    )
    assert [one.listener for one in found] == ["dashboard"]
    assert "192.168.11.10" in found[0].reason


def test_a_dashboard_bound_to_the_house_network_is_not():
    assert (
        exposure(
            listeners=[("dashboard", "192.168.1.10", 8083)],
            satellite_network="192.168.11.0/24",
            addresses=["192.168.1.10", "192.168.11.10"],
        )
        == []
    )


def test_the_media_gateway_is_meant_to_be_on_that_network():
    assert (
        exposure(
            listeners=[("media gateway", "192.168.11.10", 8084)],
            satellite_network="192.168.11.0/24",
            addresses=["192.168.1.10", "192.168.11.10"],
        )
        == []
    )


def test_the_addresses_include_the_one_the_satellites_reach_this_hub_on(monkeypatch):
    """A Raspberry Pi's host name resolves to 127.0.1.1 and nothing else.

    Judging a wildcard bind by that alone finds nothing, ever, which is the one answer this
    check must not give by accident. The route towards the nodes is asked as well.
    """
    monkeypatch.setattr(security.socket, "gethostname", lambda: "not.a.name.that.resolves")
    assert "127.0.0.1" in addresses("127.0.0.0/8")
    assert addresses(None) == []


def test_without_a_satellite_network_there_is_nothing_to_say():
    assert (
        exposure(
            listeners=[("dashboard", "0.0.0.0", 8083)],
            satellite_network=None,
            addresses=["192.168.1.10"],
        )
        == []
    )


def test_the_hub_says_so_at_startup_and_serves_anyway(tmp_path, caplog):
    from sentry_mode.config import Settings
    from sentry_mode.web import _say_what_the_satellites_can_reach

    settings = Settings(
        sentry_state_file=tmp_path / "sentry.json",
        satellites={
            "enabled": True,
            "network": "127.0.0.0/8",
            "nodes_file": tmp_path / "nodes.json",
            "store_path": tmp_path / "journal.sqlite3",
        },
    )
    with caplog.at_level("WARNING"):
        _say_what_the_satellites_can_reach(settings, "127.0.0.1", 8083, "127.0.0.1", None)
    assert "dashboard listens on 127.0.0.1" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        _say_what_the_satellites_can_reach(settings, "192.168.1.10", 8083, "192.168.1.10", None)
    assert caplog.text == ""


def test_a_network_that_is_not_one_is_refused(tmp_path):
    from pydantic import ValidationError

    from sentry_mode.satellites.config import SatellitesConfig

    assert SatellitesConfig(network="192.168.11.0/24").network == "192.168.11.0/24"
    assert SatellitesConfig(network="").network is None
    with pytest.raises(ValidationError):
        SatellitesConfig(network="the shed")

"""The Linux half of the cable: what it will carry, and what it refuses to.

The framing itself is checked in `test_satellite_link.py` and against the firmware in
`firmware/pico/tools/link_check.py`. What is here is the part of the bridge that decides
anything at all — which is deliberately almost nothing: whose message this is, what the
goodbye says, and whether a file describing which board is which can be believed.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pico_bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("pico_bridge", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Before it is executed: its dataclasses are written with postponed annotations, and
    # resolving those means looking the module up by name.
    sys.modules["pico_bridge"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def described(tmp_path) -> Path:
    where = tmp_path / "bridge.json"
    where.write_text(
        json.dumps(
            {
                "broker": {"host": "hub.local", "ca": "ca.crt"},
                "nodes": [
                    {
                        "node_id": "pico-cablato",
                        "port": "/dev/ttyACM0",
                        "certificate": "node.crt",
                        "key": "node.key",
                    }
                ],
            }
        )
    )
    return where


def test_a_board_is_the_node_its_port_is_mapped_to(bridge, described):
    broker, boards = bridge.read_configuration(described)
    assert broker.host == "hub.local" and broker.port == 8883
    assert [one.node_id for one in boards] == ["pico-cablato"]
    assert boards[0].port == "/dev/ttyACM0"


def test_a_board_with_no_identity_written_down_is_not_bridged(bridge, tmp_path):
    where = tmp_path / "bridge.json"
    where.write_text(json.dumps({"broker": {"host": "hub"}, "nodes": [{"port": "/dev/ttyACM0"}]}))
    with pytest.raises(SystemExit, match="no node_id"):
        bridge.read_configuration(where)


def test_two_ports_may_not_claim_the_same_node(bridge, tmp_path):
    where = tmp_path / "bridge.json"
    one = {
        "node_id": "pico-cablato",
        "port": "/dev/ttyACM0",
        "certificate": "c",
        "key": "k",
    }
    where.write_text(json.dumps({"broker": {"host": "hub"}, "nodes": [one, {**one, "port": "b"}]}))
    with pytest.raises(SystemExit, match="same node"):
        bridge.read_configuration(where)


def test_a_file_with_no_boards_in_it_is_not_a_bridge(bridge, tmp_path):
    where = tmp_path / "bridge.json"
    where.write_text(json.dumps({"broker": {"host": "hub"}, "nodes": []}))
    with pytest.raises(SystemExit, match="no boards"):
        bridge.read_configuration(where)


def test_who_a_message_is_about_is_read_from_where_that_message_keeps_it(bridge):
    # A state, a health report and an acknowledgement say it at the top; an event says it
    # inside the event, because the envelope around it is about the delivery.
    assert bridge.whose(b'{"node_id":"pico-cablato","online":true}') == "pico-cablato"
    assert bridge.whose(b'{"event":{"node_id":"pico-cablato"},"delivery":{}}') == "pico-cablato"
    assert bridge.whose(b"not json at all") is None
    assert bridge.whose(b"[1,2,3]") is None
    assert bridge.whose(b'{"online":true}') is None


def test_the_goodbye_is_about_the_connection_it_ends(bridge):
    state = {
        "node_id": "pico-cablato",
        "boot_id": "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611",
        "connection_id": "9b1d6e44-0f27-4a83-8c55-1d3e6a9b4c72",
        "online": True,
    }
    said = json.loads(bridge.goodbye_for(state))
    assert said["online"] is False
    assert said["connection_id"] == state["connection_id"]
    assert said["boot_id"] == state["boot_id"]
    # A hub that was sent this for a connection that has already been replaced must be able
    # to tell, which is the whole reason the connection is named in it.
    assert set(said) == {"schema_version", "node_id", "boot_id", "connection_id", "online"}

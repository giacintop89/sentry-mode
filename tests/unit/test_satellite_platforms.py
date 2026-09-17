"""What a microcontroller may be asked for, and what it may not.

A satellite used to be one kind of machine. Now that some of them have four kilobytes and
no filesystem, the hub has to know which kind it is talking to before it sends anything:
these are the checks that turn a mistake into an error on the page rather than a round
trip over MQTT that can only come back `failed`.
"""

import json
from pathlib import Path

import pytest

from sentry_mode.satellites import platforms
from sentry_mode.satellites.api import ConfigureRequest, overview
from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.control import Board
from sentry_mode.satellites.identity import NodeRegistry
from sentry_mode.satellites.mqtt import Message
from sentry_mode.satellites.service import SatelliteService
from sentry_mode.sources.registry import SourceRegistry

PICO = platforms.CATALOGUE["pico-w-sensor"]
CONNECTION = "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21"
BOOT = "6f1c2d3e-4a5b-4c7d-8e9f-0a1b2c3d4e5f"


class FakeTransport:
    """Records what the hub sends, and never opens a socket."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def publish_command(self, node_id: str, payload: bytes) -> bool:
        self.commands.append((node_id, json.loads(payload)))
        return True

    def clear_retained_state(self, node_id: str) -> bool:
        return True

    def settle(self, message) -> bool:
        return True


def message(channel: str, document: dict, node_id: str = "zero-entrance") -> Message:
    return Message(
        node_id=node_id,
        channel=channel,
        payload=json.dumps(document).encode(),
        retained=False,
        topic=f"sentry/v1/nodes/{node_id}/{channel}",
    )


def hello(sources=None) -> dict:
    return {
        "schema_version": 1,
        "node_id": "zero-entrance",
        "boot_id": BOOT,
        "connection_id": CONNECTION,
        "online": True,
        "profile": "sensor-presence",
        "sources": sources if sources is not None else [{"source_id": "pir-1", "kind": "gpio"}],
    }


# -- the catalogue itself -------------------------------------------------------


def test_every_platform_is_named_after_itself_and_can_drive_something():
    for name, one in platforms.CATALOGUE.items():
        assert one.name == name
        assert one.drivers
        assert set(one.drivers) <= set(platforms.LINUX.drivers)
        # A field a board is allowed to report has to be one the heartbeat has a place
        # for. A board may report fewer than the agent does, and — a microcontroller knows
        # why it restarted and a Linux machine does not bother to say — it may report one
        # the agent never sends.
        assert set(one.board_fields) <= set(Board.model_fields)


def test_a_node_registered_before_any_of_this_is_what_it_has_always_been():
    assert platforms.platform(None) is platforms.LINUX
    assert platforms.platform("linux-agent") is platforms.LINUX
    # A name from a registry written by a newer hub, or by a typing mistake: the node is
    # still shown, as the only kind of machine that existed when it was registered.
    assert platforms.platform("something-else-entirely") is platforms.LINUX
    assert not platforms.known("something-else-entirely")


def test_a_microcontroller_has_no_camera_and_nothing_to_test_by_hand():
    assert PICO.streams == ()
    assert PICO.manual_tests is False
    assert PICO.experimental is True
    assert "csi" not in PICO.drivers and "microphone" not in PICO.drivers


def test_the_bigger_board_runs_the_same_firmware_and_so_takes_the_same_configuration():
    # The RP2350 has more memory and more flash, and none of that moves these two numbers:
    # they are the firmware's limits — eight planned sources, and a configuration that has
    # to arrive in one packet and be kept in one sector — and it is the same firmware.
    two = platforms.CATALOGUE["pico-2w-sensor"]
    assert two.drivers == PICO.drivers
    assert two.max_sources == PICO.max_sources
    assert two.max_config_bytes == PICO.max_config_bytes
    assert two.architecture != PICO.architecture


def test_a_microcontroller_takes_what_its_firmware_can_actually_hold():
    # firmware/pico/include/sentry/plan.h: kMaxPlanned is eight on both chips. If that
    # changes, this is the other place that has to change with it.
    assert PICO.max_sources == 8
    assert PICO.max_config_bytes <= 2560  # one MQTT packet, which is what carries it


# -- what may be configured -----------------------------------------------------


def test_a_configuration_a_microcontroller_can_hold_is_accepted():
    entries = [
        {"id": "pir-1", "kind": "gpio", "pin": 17, "debounce_ms": 200},
        {"id": "temp-1", "kind": "onewire", "pin": 22},
    ]
    assert platforms.check_configuration(PICO, entries) == []


def test_a_driver_that_was_never_compiled_in_is_refused_by_name():
    wrong = platforms.check_configuration(PICO, [{"id": "cam", "kind": "csi"}])
    assert wrong == ["cam: a pico-w-sensor has no csi driver"]
    assert platforms.check_configuration(platforms.LINUX, [{"id": "cam", "kind": "csi"}]) == []


def test_an_option_naming_a_file_is_refused_because_there_is_no_filesystem():
    entry = {"id": "mic", "kind": "adc", "pin": 26, "device": "hw:1,0"}
    wrong = platforms.check_configuration(PICO, [entry])
    assert len(wrong) == 1 and "no filesystem" in wrong[0]
    # Not the name of the option but what it holds: a path is a path whatever it is called.
    entry = {"id": "led", "kind": "gpio", "pin": 17, "at": "/dev/gpio0"}
    wrong = platforms.check_configuration(PICO, [entry])
    assert len(wrong) == 1 and "led.at" in wrong[0]
    # The same options on a machine that has a filesystem are nobody's business here.
    assert (
        platforms.check_configuration(
            platforms.LINUX, [{"id": "mic", "kind": "microphone", "device": "hw:1,0"}]
        )
        == []
    )


def test_a_source_that_names_no_pin_is_refused_before_it_is_sent():
    # The firmware refuses it too, and says the same thing. The round trip is what this
    # saves: a configuration that can only come back `failed` never leaves the hub.
    wrong = platforms.check_configuration(PICO, [{"id": "pir-1", "kind": "gpio"}])
    assert wrong == ["pir-1: a gpio source needs pin, and this one names none"]
    # A board source measures the die it is on and needs nothing named.
    assert platforms.check_configuration(PICO, [{"id": "die", "kind": "board"}]) == []


def test_an_option_the_firmware_has_no_place_for_is_refused_by_the_driver_that_has_none():
    wrong = platforms.check_configuration(
        PICO, [{"id": "pir-1", "kind": "gpio", "pin": 17, "interval_seconds": 30}]
    )
    assert wrong == [
        "pir-1.interval_seconds: a gpio source on a pico-w-sensor has no interval_seconds"
    ]
    # The same word means different things to different drivers, and `device` on a 1-Wire
    # source is a probe on a bus rather than a sound card on a machine that has neither.
    assert (
        platforms.check_configuration(
            PICO, [{"id": "probe-1", "kind": "onewire", "pin": 22, "device": "28-0000071cbc42"}]
        )
        == []
    )
    # `enabled` is a field of every source rather than an option of any driver.
    assert (
        platforms.check_configuration(
            PICO, [{"id": "pir-1", "kind": "gpio", "pin": 17, "enabled": False}]
        )
        == []
    )


def test_more_sources_than_the_board_has_room_for_are_refused_before_anything_is_sent():
    entries = [
        {"id": f"pin-{index}", "kind": "gpio", "pin": index}
        for index in range(PICO.max_sources + 1)
    ]
    wrong = platforms.check_configuration(PICO, entries)
    assert wrong == [f"{len(entries)} sources: a pico-w-sensor takes at most {PICO.max_sources}"]


def test_a_configuration_too_large_to_hold_is_refused_by_its_size_on_the_wire():
    # As many sources as the board takes, each with options as long as a request allows.
    entries = [
        {
            "id": f"probe-{index}",
            "kind": "onewire",
            "pin": index,
            "device": "28-" + "0" * 125,
            "event_kind": "climate." + "x" * 120,
        }
        for index in range(PICO.max_sources)
    ]
    wrong = platforms.check_configuration(PICO, entries)
    assert len(wrong) == 1 and wrong[0].endswith(f"a pico-w-sensor holds {PICO.max_config_bytes}")
    assert platforms.check_configuration(platforms.LINUX, entries) == []


def test_the_number_measured_is_the_json_the_node_has_to_hold():
    entries = [{"id": "pir-1", "kind": "gpio", "pin": 17}]
    assert platforms._size_of(entries) == len(json.dumps(entries, separators=(",", ":")))


# -- what the page shows --------------------------------------------------------


def test_a_board_is_only_shown_the_numbers_it_could_have_taken():
    board = {
        "uptime_seconds": 61.0,
        "temperature_c": 24.5,
        "load1": None,
        "memory_available_kb": 58,
        "throttled": None,
    }
    assert platforms.visible_board(PICO, board) == {
        "uptime_seconds": 61.0,
        "temperature_c": 24.5,
        "memory_available_kb": 58,
    }
    assert platforms.visible_board(platforms.LINUX, board) == board
    assert platforms.visible_board(PICO, {}) == {}


def test_a_board_that_can_say_why_it_restarted_is_shown_saying_it():
    # The agent does not report it and a microcontroller does, so the page shows it for one
    # kind of node and not the other rather than showing a blank for both.
    board = {"uptime_seconds": 61.0, "reset": "watchdog"}
    assert platforms.visible_board(PICO, board)["reset"] == "watchdog"
    assert "reset" not in platforms.visible_board(platforms.LINUX, board)


def test_the_page_is_told_what_kinds_of_satellite_exist():
    catalogue = {one["name"]: one for one in overview({"enabled": True, "nodes": []})["platforms"]}
    assert set(catalogue) == set(platforms.CATALOGUE)
    assert catalogue["pico-w-sensor"]["experimental"] is True
    assert catalogue["pico-w-sensor"]["streams"] == []
    assert catalogue["linux-agent"]["drivers"] == list(platforms.LINUX.drivers)


# -- the registry ---------------------------------------------------------------


def test_a_node_is_registered_as_a_kind_of_machine(tmp_path):
    nodes = NodeRegistry(tmp_path / "nodes.json")
    record = nodes.register("pico-ingresso", platform="pico-w-sensor")
    assert record.platform == "pico-w-sensor"
    assert nodes.register("zero-garden").platform == platforms.DEFAULT


def test_a_kind_of_machine_this_hub_has_never_heard_of_is_refused(tmp_path):
    nodes = NodeRegistry(tmp_path / "nodes.json")
    with pytest.raises(ValueError, match="not a kind of satellite"):
        nodes.register("pico-ingresso", platform="esp32")
    nodes.register("pico-ingresso")
    with pytest.raises(ValueError, match="not a kind of satellite"):
        nodes.describe("pico-ingresso", platform="esp32")


def test_a_node_that_turned_out_to_be_a_microcontroller_can_be_corrected(tmp_path):
    nodes = NodeRegistry(tmp_path / "nodes.json")
    nodes.register("pico-ingresso")
    assert nodes.describe("pico-ingresso", platform="pico-2w-sensor").platform == "pico-2w-sensor"
    assert NodeRegistry(tmp_path / "nodes.json").require("pico-ingresso").platform == (
        "pico-2w-sensor"
    )


def test_a_registry_written_before_this_field_existed_still_loads(tmp_path):
    path = tmp_path / "nodes.json"
    path.write_text(
        json.dumps(
            {
                "revision": 3,
                "hub_epoch": 2,
                "nodes": {
                    "zero-entrance": {
                        "node_id": "zero-entrance",
                        "display_name": "Ingresso",
                        "profile": "sensor-presence",
                        "status": "approved",
                        "fingerprint": "a" * 64,
                        "registered_at": "2026-01-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert NodeRegistry(path).require("zero-entrance").platform == platforms.DEFAULT


# -- the service ----------------------------------------------------------------


@pytest.fixture
def pico(tmp_path) -> SatelliteService:
    config = SatellitesConfig(
        enabled=True,
        nodes_file=tmp_path / "nodes.json",
        store_path=tmp_path / "journal.sqlite3",
    )
    nodes = NodeRegistry(config.nodes_file)
    nodes.register("zero-entrance", display_name="Ingresso", platform="pico-w-sensor")
    nodes.approve("zero-entrance", pinned="a" * 64)
    built = SatelliteService(
        config, sources=SourceRegistry(), nodes=nodes, transport=FakeTransport()
    )
    built.sessions.broker_connected()
    built.store.open()
    built.handle(message("state", hello()))
    yield built
    built.store.close()


def test_a_camera_is_never_sent_to_a_board_that_has_no_camera_port(pico):
    with pytest.raises(ValueError, match="no csi driver"):
        pico.configure("zero-entrance", [{"id": "cam", "kind": "csi"}])
    assert [
        command for _, command in pico._transport.commands if command["action"] == "configure"
    ] == []


def test_what_the_board_can_drive_is_still_sent(pico):
    sent = pico.configure("zero-entrance", [{"id": "pir-1", "kind": "gpio", "pin": 17}])
    assert sent["revision"] == 1
    node, command = pico._transport.commands[-1]
    assert command["action"] == "configure"
    assert command["sources"] == [{"id": "pir-1", "kind": "gpio", "pin": 17}]


def test_the_page_says_what_kind_of_machine_each_node_is(pico):
    node = overview(pico.status())["nodes"][0]
    assert node["platform"]["name"] == "pico-w-sensor"
    assert node["platform"]["experimental"] is True
    assert node["platform"]["manual_tests"] is False


def test_a_source_of_a_kind_this_board_cannot_drive_is_shown_as_unsupported(pico):
    pico.handle(
        message(
            "state",
            dict(hello(), sources=[{"source_id": "cam", "kind": "csi", "enabled": True}]),
        )
    )
    node = overview(pico.status())["nodes"][0]
    shown = {source["name"]: source for source in node["sources"]}
    assert shown["cam"]["supported"] is False


# -- the administrative tool ----------------------------------------------------


def test_the_kinds_of_satellite_can_be_read_from_a_shell(capsys, tmp_path):
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "satellite_admin_platforms", root / "scripts" / "satellite_admin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--nodes-file", str(tmp_path / "nodes.json"), "platforms", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [one["name"] for one in listed] == list(platforms.CATALOGUE)


def test_a_node_can_be_registered_as_a_microcontroller_from_a_shell(capsys, tmp_path):
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "satellite_admin_register", root / "scripts" / "satellite_admin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "nodes.json"
    assert (
        module.main(
            [
                "--nodes-file",
                str(path),
                "register",
                "--node",
                "pico-1",
                "--platform",
                "pico-w-sensor",
            ]
        )
        == 0
    )
    assert "experimental" in capsys.readouterr().out
    assert NodeRegistry(path).require("pico-1").platform == "pico-w-sensor"


# -- what the request model refuses ---------------------------------------------


def test_a_kind_no_satellite_has_a_driver_for_never_becomes_a_request():
    with pytest.raises(ValueError, match="cannot be configured"):
        ConfigureRequest(node_id="pico-1", sources=[{"id": "x", "kind": "lidar"}])

"""Sources configured from the hub: checked first, applied by revision, undone on failure."""

import json
import time
from pathlib import Path

import pytest
from unit.test_agent import FakeTransport, config, identity, until

from sentry_satellite import commands, protocol, remote
from sentry_satellite.agent import Agent
from sentry_satellite.drivers import Builder, Hardware
from sentry_satellite.sensors.gpio import LineError

COMMANDS = protocol.topic("zero-entrance", "commands")


class QuietLine:
    def __init__(self):
        self.released = False

    def level(self):
        return False

    def wait(self, timeout):
        time.sleep(min(timeout, 0.005))
        return False

    def edges(self):
        return []

    def release(self):
        self.released = True


class Lines:
    """Hands out quiet lines, except on the lines it was told are taken."""

    def __init__(self, busy=()):
        self.busy = set(busy)
        self.opened: list[int] = []

    def __call__(self, chip, line, *, consumer, bias):
        if line in self.busy:
            raise LineError(f"line {line} is busy (held by another-program)")
        self.opened.append(line)
        return QuietLine()


def pir(source_id="pir-1", line=17, **options):
    return {"id": source_id, "kind": "gpio", "line_numbering": "bcm", "line": line, **options}


def dummy(source_id="sim-1"):
    return {"id": source_id, "kind": "dummy", "interval_seconds": 60}


def make(tmp_path, lines=None, *, overlay=True, builder=True):
    lines = lines or Lines()
    transport = FakeTransport()
    agent = Agent(
        config=config(),
        identity=identity(),
        transport=transport,
        drivers=[],
        idle_seconds=0.01,
        reconnect_seconds=0.01,
        join_seconds=1.0,
        builder=Builder(lambda: Hardware(open_line=lines)) if builder else None,
        overlay=remote.Overlay(tmp_path / "state" / "sources.json") if overlay else None,
        probe_seconds=0.2,
    )
    agent.start()
    return agent, transport, lines


def configure(transport, revision, sources, command_id=None):
    transport.deliver(
        COMMANDS,
        {
            "command_id": command_id or f"cfg-{revision}",
            "action": "configure",
            "node_id": "zero-entrance",
            "hub_epoch": 3,
            "revision": revision,
            "sources": sources,
        },
    )


def outcomes(transport, command_id):
    return [
        (ack["outcome"], ack["detail"])
        for ack in transport.on("acks")
        if ack["command_id"] == command_id
    ]


def settled(transport, command_id):
    return until(lambda: any(o != "received" for o, _ in outcomes(transport, command_id)))


def test_a_new_list_of_sources_is_applied_and_kept(tmp_path):
    agent, transport, lines = make(tmp_path)
    configure(transport, 1, [pir(), dummy()])
    assert settled(transport, "cfg-1")
    assert outcomes(transport, "cfg-1") == [("received", None), ("applied", "revision 1")]
    assert [driver.source_id for driver in agent.drivers] == ["pir-1", "sim-1"]
    assert until(lambda: lines.opened == [17])
    kept = json.loads((tmp_path / "state" / "sources.json").read_text())
    assert kept["revision"] == 1 and kept["node_id"] == "zero-entrance"
    assert [entry["id"] for entry in kept["sources"]] == ["pir-1", "sim-1"]
    assert (tmp_path / "state" / "sources.json").stat().st_mode & 0o777 == 0o600
    state = [message for message in transport.on("state") if message["config_revision"] == 1]
    declared = state[-1]["sources"][0]
    assert (declared["source_id"], declared["kind"], declared["enabled"]) == ("pir-1", "gpio", True)
    assert declared["options"]["line"] == 17 and "enabled" not in declared["options"]
    agent.stop()
    assert agent.stopped_cleanly


def test_the_old_drivers_stop_before_the_new_ones_take_the_line(tmp_path):
    agent, transport, _ = make(tmp_path)
    configure(transport, 1, [pir()])
    assert settled(transport, "cfg-1")
    first = agent.drivers[0]
    configure(transport, 2, [pir(debounce_ms=10)])
    assert settled(transport, "cfg-2")
    assert outcomes(transport, "cfg-2")[-1] == ("applied", "revision 2")
    assert agent.drivers[0] is not first
    assert first._stopped.is_set()
    agent.stop()


@pytest.mark.parametrize(
    ("sources", "reason"),
    [
        ([{"id": "tag-1", "kind": "ble"}], "no ble driver installed"),
        ([pir("a"), pir("b")], "both use BCM line 17"),
        ([pir(line=2), {"id": "t", "kind": "bme280", "measure": "temperature"}], "i2c-1"),
        ([pir(line=99)], "line must be from 0 to 53"),
        ([pir(colour="red")], "does not take colour"),
        ([{"id": "hub.pir", "kind": "gpio"}], "sources 1"),
    ],
)
def test_a_request_that_fails_the_checks_changes_nothing(tmp_path, sources, reason):
    agent, transport, lines = make(tmp_path)
    configure(transport, 1, [dummy()])
    assert settled(transport, "cfg-1")
    before = list(agent.drivers)
    configure(transport, 2, sources)
    assert settled(transport, "cfg-2")
    outcome, detail = outcomes(transport, "cfg-2")[-1]
    assert outcome == "failed" and reason in detail
    assert agent.drivers == before and agent.revision == 1
    assert json.loads((tmp_path / "state" / "sources.json").read_text())["revision"] == 1
    agent.stop()


def test_a_revision_that_is_not_newer_is_refused(tmp_path):
    agent, transport, _ = make(tmp_path)
    configure(transport, 3, [dummy()])
    assert settled(transport, "cfg-3")
    configure(transport, 3, [pir()], command_id="again")
    assert settled(transport, "again")
    assert outcomes(transport, "again")[-1] == ("failed", "revision 3 is not newer than 3")
    agent.stop()


def test_a_driver_that_cannot_start_puts_the_previous_sources_back(tmp_path):
    lines = Lines(busy={22})
    agent, transport, _ = make(tmp_path, lines)
    configure(transport, 1, [pir()])
    assert settled(transport, "cfg-1")
    configure(transport, 2, [pir(), pir("door", line=22, event_kind="sensor.contact")])
    assert settled(transport, "cfg-2")
    outcome, detail = outcomes(transport, "cfg-2")[-1]
    assert outcome == "failed"
    assert "door: line 22 is busy" in detail and "revision 1 is still in use" in detail
    assert [driver.source_id for driver in agent.drivers] == ["pir-1"]
    assert agent.revision == 1
    assert until(lambda: lines.opened.count(17) == 3)  # first, during the attempt, restored
    assert json.loads((tmp_path / "state" / "sources.json").read_text())["revision"] == 1
    assert transport.on("state")[-1]["config_revision"] == 1
    agent.stop()


def test_a_configuration_that_cannot_be_kept_is_not_left_running(tmp_path):
    agent, transport, _ = make(tmp_path)
    (tmp_path / "state").write_text("a file where the directory should be")
    configure(transport, 1, [pir()])
    assert settled(transport, "cfg-1")
    outcome, detail = outcomes(transport, "cfg-1")[-1]
    assert outcome == "failed" and "could not be kept" in detail
    assert agent.drivers == [] and agent.revision == 0
    agent.stop()


def test_a_repeated_command_is_answered_and_not_applied_twice(tmp_path):
    agent, transport, lines = make(tmp_path)
    configure(transport, 1, [pir()])
    assert settled(transport, "cfg-1")
    driver = agent.drivers[0]
    configure(transport, 1, [pir()])
    time.sleep(0.3)
    assert outcomes(transport, "cfg-1")[-1] == ("applied", "already handled")
    assert agent.drivers[0] is driver
    assert lines.opened == [17]
    agent.stop()


def test_a_repeat_while_the_first_is_still_starting_hears_received(tmp_path):
    agent, transport, _ = make(tmp_path)
    configure(transport, 1, [pir()])
    configure(transport, 1, [pir()])
    assert ("received", "already handled") in outcomes(transport, "cfg-1")
    assert settled(transport, "cfg-1")
    agent.stop()


def test_a_second_request_during_the_first_is_refused_and_can_be_sent_again(tmp_path):
    agent, transport, _ = make(tmp_path)
    configure(transport, 1, [pir()])
    configure(transport, 2, [dummy()])
    assert outcomes(transport, "cfg-2") == [("failed", "another configuration is being applied")]
    assert settled(transport, "cfg-1")
    configure(transport, 2, [dummy()])
    assert until(lambda: outcomes(transport, "cfg-2")[-1] == ("applied", "revision 2"))
    agent.stop()


def test_a_node_without_remote_configuration_says_so(tmp_path):
    agent, transport, _ = make(tmp_path, builder=False)
    configure(transport, 1, [pir()])
    assert settled(transport, "cfg-1")
    assert outcomes(transport, "cfg-1")[-1] == (
        "failed",
        "this node does not take its sources from the hub",
    )
    agent.stop()


# -- the overlay on disk -------------------------------------------------------------------


def test_the_overlay_replaces_the_installed_sources_on_the_next_start(tmp_path):
    overlay = remote.Overlay(tmp_path / "sources.json")
    installed = config()
    wanted = remote.check(installed, 4, [pir()], current=0, kinds=Builder.kinds)
    overlay.save(wanted, 4)
    applied = overlay.load(installed, kinds=Builder.kinds)
    assert applied.revision == 4
    assert [source.id for source in applied.config.sources] == ["pir-1"]
    assert applied.config.hub == installed.hub and applied.config.tls == installed.tls


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        json.dumps([]),
        json.dumps({"node_id": "zero-garden", "revision": 1, "sources": []}),
        json.dumps({"node_id": "zero-entrance", "revision": True, "sources": []}),
        json.dumps({"node_id": "zero-entrance", "revision": 2, "sources": [{"kind": "csi"}]}),
        json.dumps(
            {"node_id": "zero-entrance", "revision": 2, "sources": [{"id": "x", "kind": "ble"}]}
        ),
    ],
)
def test_an_unusable_overlay_is_set_aside(tmp_path, document, caplog):
    path = tmp_path / "sources.json"
    path.write_text(document)
    installed = config()
    applied = remote.Overlay(path).load(installed, kinds=Builder.kinds)
    assert applied.revision == 0 and applied.config is installed
    assert "ignoring" in caplog.text


def test_no_overlay_means_the_installed_file(tmp_path):
    installed = config()
    applied = remote.Overlay(Path(tmp_path / "missing.json")).load(installed, kinds=("gpio",))
    assert applied == remote.Applied(0, installed)


# -- the command itself --------------------------------------------------------------------


def base(**changes):
    return {"command_id": "c", "node_id": "zero-entrance", "hub_epoch": 1, **changes}


def test_a_configure_command_is_numbered_and_carries_a_list():
    parsed = commands.parse(
        base(action="configure", revision=2, sources=[dummy()]), node_id="zero-entrance"
    )
    assert parsed.revision == 2 and parsed.sources == (dummy(),)


@pytest.mark.parametrize(
    "message",
    [
        base(action="configure", sources=[]),
        base(action="configure", revision=0, sources=[]),
        base(action="configure", revision=True, sources=[]),
        base(action="configure", revision=1, sources={"id": "x"}),
        base(action="configure", revision=1, sources=["x"]),
        base(action="configure", revision=1, sources=[dummy()] * 33),
        base(action="grant", capability="events", grant_id="g", sources=[]),
        base(action="stop", revision=1),
        base(action="configure", revision=1, sources=[], network={"ssid": "x"}),
    ],
)
def test_a_malformed_configure_command_is_refused(message):
    with pytest.raises(commands.CommandError):
        commands.parse(message, node_id="zero-entrance")

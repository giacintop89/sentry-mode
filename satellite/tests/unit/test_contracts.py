"""The satellite holds itself to the schema the hub publishes, with no shared code."""

import json
from pathlib import Path

import pytest
from conftest import CONTRACTS, REPOSITORY
from schema import Invalid, validate

from sentry_satellite import names, protocol

pytestmark = pytest.mark.skipif(not CONTRACTS.is_dir(), reason="the contracts are not beside us")

SCHEMA = json.loads((CONTRACTS / "event.schema.json").read_text()) if CONTRACTS.is_dir() else {}
VALID = sorted((CONTRACTS / "fixtures/valid").glob("*.json")) if CONTRACTS.is_dir() else []
INVALID = sorted((CONTRACTS / "fixtures/invalid").glob("*.json")) if CONTRACTS.is_dir() else []


def properties(*path: str) -> dict:
    node = SCHEMA
    for name in path:
        if "$ref" in node:
            node = SCHEMA["$defs"][node["$ref"].split("/")[-1]]
        node = node["properties"][name]
    return node


def test_the_hub_and_the_agent_spell_a_name_the_same_way():
    assert properties("event", "node_id")["pattern"] == names.NAME
    assert properties("event", "source_id")["pattern"] == names.NAME
    assert properties("event", "kind")["pattern"] == names.KIND


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_what_the_hub_accepts_the_agent_also_accepts(path):
    validate(json.loads(path.read_text()), SCHEMA)


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.stem)
def test_what_the_hub_refuses_the_agent_also_refuses(path):
    with pytest.raises(Invalid):
        validate(json.loads(path.read_text()), SCHEMA)


def test_what_the_agent_builds_is_what_the_hub_expects():
    from datetime import UTC, datetime

    events = protocol.Events("zero-entrance", "2c9a7f38-16d4-4b9e-9a0c-77f0b2d5e611")
    event = events.event(
        "pir-1",
        "sensor.motion",
        True,
        occurred_at=datetime(2026, 9, 16, 21, 4, 7, tzinfo=UTC),
        clock_status="synced",
    )
    message = protocol.envelope(
        event,
        protocol.Delivery(
            connection_id="9b1d6e44-0f27-4a83-8c55-1d3e7a9042bb", hub_epoch=7, grant_id=None
        ),
    )
    validate(message, SCHEMA)
    assert protocol.encode(message)


def load_simulator():
    import importlib.util

    path = REPOSITORY / "scripts" / "satellite_simulator.py"
    if not path.exists():
        pytest.skip("the simulator is not beside us")
    spec = importlib.util.spec_from_file_location("satellite_simulator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_simulated_scenario_is_something_the_hub_could_really_receive():
    simulator = load_simulator()
    for name in simulator.SCENARIOS:
        messages = simulator.generate(name)
        assert messages
        for message in messages:
            validate(message, SCHEMA)


def test_a_scenario_is_the_same_every_time_it_is_run():
    simulator = load_simulator()
    assert simulator.generate("backlog") == simulator.generate("backlog")
    assert simulator.generate("backlog", seed=1) != simulator.generate("backlog", seed=2)


def test_a_simulated_event_carries_a_reading_and_nothing_else():
    simulator = load_simulator()
    text = json.dumps(simulator.generate("all"))
    for forbidden in ("ssh", "exec", "command", "rule", "/bin/", "sudo", "http://", "https://"):
        assert forbidden not in text


def test_the_checker_refuses_what_it_does_not_understand():
    with pytest.raises(Invalid):
        validate({}, {"allOf": []})


def test_a_missing_contract_directory_is_a_skip_not_a_pass():
    assert Path(CONTRACTS).is_dir()

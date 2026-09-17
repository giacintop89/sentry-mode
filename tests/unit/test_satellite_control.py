"""The other four channels, held to the same standard as the events topic.

State, health, commands and acks were written by one side and read by the other with a
default for anything missing. These tests are what turns that into a contract: the same
fixtures as the agent checks, the same verdicts, a schema generated rather than written,
and proof that what this hub really sends is something the contract allows.
"""

import json
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from sentry_mode.satellites.control import (
    MESSAGES,
    RESETS,
    CommandAck,
    HealthReport,
    HubCommand,
    NodeState,
    control_schema,
)
from sentry_mode.satellites.sessions import Grant

CONTROL = Path(__file__).resolve().parents[2] / "contracts/satellite/v1/control"
VALID = sorted((CONTROL / "fixtures/valid").glob("*.json"))
INVALID = sorted((CONTROL / "fixtures/invalid").glob("*.json"))

WILL_LIMIT = 255
"""What an lwIP will has room for: the client writes its lengths in a single byte."""


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def model_for(path: Path):
    """Which message a fixture is, by the name it was filed under."""
    return MESSAGES[path.stem.partition("-")[0]]


def test_there_are_fixtures_for_all_four_channels():
    assert {path.stem.partition("-")[0] for path in VALID} == set(MESSAGES)
    assert INVALID


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_a_message_both_sides_agreed_on_is_accepted(path):
    model_for(path).model_validate(load(path))


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.stem)
def test_a_message_neither_side_should_accept_is_refused(path):
    with pytest.raises(ValidationError):
        model_for(path).model_validate(load(path))


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_reading_a_message_and_writing_it_back_changes_nothing(path):
    """Written back as it arrived: the fields it carried, and not the ones it did not.

    A command is defined by what it carries. Filling in the defaults would turn a grant
    into a grant that also mentions a configuration, which is a message the node refuses
    on purpose, so a message is written back with the fields that were set on it.
    """
    model = model_for(path)
    once = model.model_validate(load(path))
    assert model.model_validate(once.model_dump(mode="json", exclude_unset=True)) == once


@pytest.mark.parametrize("name", sorted(MESSAGES), ids=str)
def test_the_published_schema_still_matches_the_model_it_came_from(name):
    committed = json.loads((CONTROL / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert committed == control_schema(name), (
        "run scripts/generate_contracts.py: the models and the published schemas disagree"
    )


# -- what a small node has to be able to say -------------------------------------------


def test_a_goodbye_fits_in_a_will_a_microcontroller_can_register():
    """The lwIP client writes the will's topic and payload with one length byte each.

    The full snapshot does not fit and does not have to: a node that is leaving has to say
    who it is and that it is gone, and the hub reads exactly those fields before it closes
    the session. A node whose goodbye was truncated would be left looking online forever.
    """
    goodbye = load(CONTROL / "fixtures/valid/state-goodbye.json")
    state = NodeState.model_validate(goodbye)
    assert state.online is False
    assert state.sources == []
    payload = json.dumps(goodbye, separators=(",", ":")).encode("utf-8")
    topic = f"sentry/v1/nodes/{state.node_id}/state".encode()
    assert len(payload) <= WILL_LIMIT
    assert len(topic) <= WILL_LIMIT


def test_a_board_that_cannot_measure_itself_says_so_rather_than_saying_zero():
    report = HealthReport.model_validate(
        load(CONTROL / "fixtures/valid/health-a-board-that-cannot-measure-itself.json")
    )
    assert report.board.temperature_c is None
    assert report.board.load1 is None
    assert report.clock_status.value == "unsynced"


def test_a_board_says_why_it_came_back_in_words_the_hub_knows():
    """A restart is evidence, and which restart it was decides who has to fix it.

    The vocabulary is closed on purpose even though health is the open channel: a node
    that cannot tell leaves the field out, and a node that writes a word of its own has
    written something no page can turn into an answer.
    """
    said = load(CONTROL / "fixtures/valid/health-a-board-that-says-why-it-came-back.json")
    assert HealthReport.model_validate(said).board.reset == "watchdog"
    for word in RESETS:
        assert HealthReport.model_validate({**said, "board": {**said["board"], "reset": word}})
    assert "unknown" not in RESETS
    # The agent does not say, and that is not the same as a clean start.
    silent = load(CONTROL / "fixtures/valid/health-heartbeat.json")
    assert HealthReport.model_validate(silent).board.reset is None


def test_health_keeps_a_counter_it_has_never_heard_of():
    """Diagnostics are the one channel that is open, and this is why.

    A board reports what it can measure. A hub that refused the whole heartbeat because one
    counter was new would lose the reading it most needs at the moment a new kind of node
    arrives, so an unknown field is kept and shown rather than thrown away.
    """
    document = load(CONTROL / "fixtures/valid/health-heartbeat.json")
    document["board"]["free_heap_bytes"] = 58112
    document["sources"]["pir-1"]["interrupts"] = 4
    report = HealthReport.model_validate(document)
    assert report.board.model_extra == {"free_heap_bytes": 58112}
    assert report.sources["pir-1"].model_extra == {"interrupts": 4}


def test_a_node_may_not_claim_a_source_on_another_node():
    document = load(CONTROL / "fixtures/valid/state-online.json")
    document["sources"][0]["source_id"] = "zero-entrance.pir-1"
    with pytest.raises(ValidationError):
        NodeState.model_validate(document)


def test_a_node_says_when_something_else_is_carrying_what_it_says():
    # A satellite with no radio is plugged into a machine that publishes for it. The node
    # is what says so — the bridge forwards what it is handed and writes nothing into it —
    # and there is one word for it, so that "reached over a cable" cannot arrive spelled
    # five ways.
    document = load(CONTROL / "fixtures/valid/state-online.json")
    assert NodeState.model_validate(document).reached_by is None
    assert NodeState.model_validate({**document, "reached_by": "bridge"}).reached_by == "bridge"
    with pytest.raises(ValidationError):
        NodeState.model_validate({**document, "reached_by": "usb"})


def test_a_state_is_refused_for_more_sources_than_a_node_may_have():
    document = load(CONTROL / "fixtures/valid/state-online.json")
    document["sources"] = [{"source_id": f"pir-{index}", "kind": "gpio"} for index in range(1, 35)]
    with pytest.raises(ValidationError):
        NodeState.model_validate(document)


# -- the closed grammar of a command ----------------------------------------------------


def test_the_grant_this_hub_really_sends_is_one_the_contract_allows():
    grant = Grant(
        grant_id=str(uuid.uuid4()),
        node_id="pico-ingresso",
        capability="events",
        hub_epoch=7,
        issued_at=1000.0,
        expires_at=1300.0,
    )
    command = HubCommand.model_validate(grant.as_command())
    assert command.action == "grant"
    assert command.duration_seconds == 300.0
    assert HubCommand.model_validate(grant.as_command(action="revoke")).action == "revoke"


def test_a_command_carries_only_what_its_action_is_about():
    stop = load(CONTROL / "fixtures/valid/command-audio-stop.json")
    HubCommand.model_validate(stop)
    for extra in ({"source_id": "mic-1"}, {"port": 8555}, {"revision": 2}):
        with pytest.raises(ValidationError):
            HubCommand.model_validate({**stop, **extra})


def test_a_stream_lasts_a_while_and_not_forever():
    start = load(CONTROL / "fixtures/valid/command-video-start.json")
    for seconds in (0, 601):
        with pytest.raises(ValidationError):
            HubCommand.model_validate({**start, "duration_seconds": seconds})
    assert HubCommand.model_validate({**start, "duration_seconds": 600}).duration_seconds == 600


def test_a_command_cannot_name_a_program_a_file_or_another_host():
    grant = load(CONTROL / "fixtures/valid/command-grant.json")
    for smuggled in ("run", "command", "url", "host", "path", "script"):
        with pytest.raises(ValidationError):
            HubCommand.model_validate({**grant, smuggled: "/bin/sh"})


def test_a_configured_source_is_a_driver_with_plain_options():
    configure = load(CONTROL / "fixtures/valid/command-configure.json")
    command = HubCommand.model_validate(configure)
    assert [source.id for source in command.sources] == ["pir-1", "door-1"]
    assert command.sources[0].model_extra == {"pin": 17, "debounce_ms": 200}
    nested = {**configure, "sources": [{"id": "pir-1", "kind": "gpio", "pin": {"gpio": 17}}]}
    with pytest.raises(ValidationError):
        HubCommand.model_validate(nested)


def test_an_answer_is_about_one_command_and_says_what_became_of_it():
    ack = CommandAck.model_validate(load(CONTROL / "fixtures/valid/ack-failed.json"))
    assert ack.outcome == "failed"
    assert ack.detail and ack.detail.startswith("pir-1")
    with pytest.raises(ValidationError):
        CommandAck.model_validate(
            {**load(CONTROL / "fixtures/valid/ack-applied.json"), "detail": "x" * 257}
        )


# -- what this hub really puts on the wire ----------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))  # the service double lives beside this file

from test_satellite_service import (  # noqa: E402, F401 - fixtures, shared with the service tests
    approve,
    hello,
    message,
    service,
)


def test_every_command_a_session_produces_is_one_the_contract_allows(service):  # noqa: F811
    """Through the service, not through a model: this is what a node would receive.

    A grant on the way in, a configuration sent afterwards, and the revocation that ends
    it all come out of `_command`, which fills in the envelope around whatever asked for
    them. That filling in is the part no unit test of the models would ever see.
    """
    approve(service)
    service.handle(message("state", hello()))
    service.configure("zero-entrance", [{"id": "pir-1", "kind": "gpio", "pin": 17}])
    service.revoke("zero-entrance", reason="sold")

    sent = [HubCommand.model_validate(command) for _, command in service._transport.commands]
    assert [command.action for command in sent] == ["grant", "configure", "revoke"]
    assert sent[0].capability == "events"
    assert sent[1].revision == 1
    assert {command.node_id for command in sent} == {"zero-entrance"}


def test_the_hello_the_hub_is_tested_against_is_a_message_a_node_may_send(service):  # noqa: F811
    NodeState.model_validate(hello())

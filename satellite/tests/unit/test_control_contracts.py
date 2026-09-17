"""The agent is held to the control schemas the same way it is held to the event one.

The hub validates these four messages with Pydantic. The agent has no Pydantic, so it
checks the published schema itself, and its command parser — the closed grammar that
decides what this node will act on — is checked against the same fixtures. Same files,
same verdicts, and what the agent really publishes is measured against the contract
rather than against another copy of the agent's own opinion.
"""

import json
import uuid

import pytest
from conftest import CONTRACTS
from schema import KNOWN, Invalid, validate
from test_agent import build, until

from sentry_satellite import commands

pytestmark = pytest.mark.skipif(not CONTRACTS.is_dir(), reason="the contracts are not beside us")

CONTROL = CONTRACTS / "control"
SCHEMAS = (
    {path.stem.partition(".")[0]: json.loads(path.read_text()) for path in CONTROL.glob("*.json")}
    if CONTROL.is_dir()
    else {}
)
VALID = sorted((CONTROL / "fixtures/valid").glob("*.json")) if CONTROL.is_dir() else []
INVALID = sorted((CONTROL / "fixtures/invalid").glob("*.json")) if CONTROL.is_dir() else []


def kind_of(path) -> str:
    return path.stem.partition("-")[0]


def load(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_there_is_a_schema_for_each_channel():
    assert set(SCHEMAS) == {"state", "health", "command", "ack"}


@pytest.mark.parametrize("path", VALID, ids=lambda path: path.stem)
def test_what_the_hub_accepts_the_agent_also_accepts(path):
    document = load(path)
    validate(document, SCHEMAS[kind_of(path)])
    if kind_of(path) == "command":
        # The schema says what shape a command has; this says the node would act on it.
        commands.parse(document, node_id=document["node_id"])


@pytest.mark.parametrize("path", INVALID, ids=lambda path: path.stem)
def test_what_the_hub_refuses_the_agent_also_refuses(path):
    """A command is refused by the grammar, the other three by the schema.

    Which field a command may carry depends on what it is asking for, and that is a rule
    the agent enforces in code rather than one a schema can state. It is the parser that
    has to agree with the hub, so it is the parser that is asked.
    """
    document = load(path)
    if kind_of(path) == "command":
        with pytest.raises(commands.CommandError):
            commands.parse(document, node_id=document["node_id"])
        return
    with pytest.raises(Invalid):
        validate(document, SCHEMAS[kind_of(path)])


# -- what this agent really publishes ---------------------------------------------------


def running_agent():
    agent, transport = build()
    agent.start()
    assert until(lambda: transport.on("state") and transport.on("health"))
    agent.stop()
    return transport


def test_the_snapshot_the_agent_publishes_is_one_the_hub_could_read():
    transport = running_agent()
    hello = transport.on("state")[0]
    validate(hello, SCHEMAS["state"])
    assert hello["online"] is True


def test_the_goodbye_the_agent_leaves_with_the_broker_says_who_is_leaving():
    transport = running_agent()
    topic, will = transport.will
    validate(will, SCHEMAS["state"])
    assert will["online"] is False
    assert topic.endswith("/state")


def test_the_heartbeat_the_agent_publishes_is_one_the_hub_could_read():
    transport = running_agent()
    beat = transport.on("health")[0]
    validate(beat, SCHEMAS["health"])
    assert beat["queue"]["granted"] is False


def test_the_answer_the_agent_gives_is_one_the_hub_could_read():
    document = load(CONTROL / "fixtures/valid/command-grant.json")
    command = commands.parse(document, node_id=document["node_id"])
    for outcome, detail in (("received", None), ("applied", None), ("failed", "no such driver")):
        answer = commands.ack(command, outcome, detail=detail)
        validate(answer, SCHEMAS["ack"])
        assert answer["command_id"] == document["command_id"]


def test_a_command_meant_for_another_node_is_not_acted_on_here():
    """The schema cannot know who is reading, and the node can. Both checks are needed.

    A command is addressed, and the address is checked against this node's own identity
    rather than against the one written in the message, which is what stops a command
    published to the wrong topic from being obeyed by whoever happens to see it.
    """
    document = load(CONTROL / "fixtures/valid/command-grant.json")
    validate(document, SCHEMAS["command"])
    with pytest.raises(commands.CommandError, match="addressed to"):
        commands.parse(document, node_id="somewhere-else")


def test_the_checker_understands_every_keyword_these_schemas_use():
    """A checker that skipped what it did not recognise would pass anything.

    The event schema needed a small subset of JSON Schema. These need maps and bounded
    lists as well, so the subset grew, and this is the test that the growth was real: the
    checker refuses a schema with a keyword it does not implement rather than ignoring it.
    """
    for name, document in SCHEMAS.items():
        with pytest.raises(Invalid, match="is required|does not take"):
            validate({"nothing": str(uuid.uuid4())}, document)
        assert keywords(document) <= KNOWN, f"{name} uses something the checker cannot check"


def keywords(node, found=None) -> set:
    """Every JSON Schema keyword in a schema, without the property names it describes."""
    found = set() if found is None else found
    if isinstance(node, dict):
        found |= set(node) & KNOWN | {name for name in node if name.startswith(("$", "max", "min"))}
        for name, value in node.items():
            if name in ("properties", "$defs"):
                for entry in value.values():
                    keywords(entry, found)
            else:
                keywords(value, found)
    elif isinstance(node, list):
        for value in node:
            keywords(value, found)
    return found

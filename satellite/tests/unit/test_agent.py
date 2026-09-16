"""The agent as a peripheral: it reports, it obeys a grant, and it stops when told."""

import json
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentry_satellite import protocol
from sentry_satellite.agent import Agent
from sentry_satellite.config import parse
from sentry_satellite.identity import Identity
from sentry_satellite.sensors import Reading
from sentry_satellite.sensors.dummy import Motion, Scripted, Stalled

CONFIG = """
[node]
id = "zero-entrance"
profile = "sensor-presence"

[hub]
mqtt_host = "192.168.11.10"

[tls]
ca_file = "/etc/sentry-satellite/ca.crt"
cert_file = "/etc/sentry-satellite/node.crt"
key_file = "/etc/sentry-satellite/node.key"

[limits]
event_max_count = 8
event_max_bytes = 4096
event_max_age_seconds = 60
heartbeat_seconds = 1
"""


class FakeTransport:
    """A link that records instead of connecting, and can hand the agent a command."""

    def __init__(self, *, refuse_publish: bool = False) -> None:
        self.connection_id = str(uuid.uuid4())
        self.published: list[tuple[str, dict, int, bool]] = []
        self.refuse_publish = refuse_publish
        self.handlers: dict = {}
        self._connected = False
        self.disconnects = 0
        self.will: tuple[str, dict] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def set_will(self, topic, payload) -> None:
        self.will = (topic, json.loads(payload))

    def connect(self, connection_id) -> None:
        self.connection_id = connection_id
        self._connected = True

    def subscribe(self, topic, handler) -> None:
        self.handlers[topic] = handler

    def publish(self, topic, payload, qos=1, retain=False) -> bool:
        if self.refuse_publish:
            return False
        self.published.append((topic, json.loads(payload), qos, retain))
        return True

    def disconnect(self) -> None:
        self._connected = False
        self.disconnects += 1

    def deliver(self, topic, message: dict) -> None:
        self.handlers[topic](topic, json.dumps(message).encode())

    def on(self, channel: str) -> list[dict]:
        return [message for topic, message, _, _ in self.published if topic.endswith(channel)]


def config(**changes):
    document = tomllib.loads(CONFIG)
    document.setdefault("sources", [])
    for key, value in changes.items():
        document["limits"][key] = value
    return parse(document, identity_file=Path("/nowhere/identity.json"))


def identity() -> Identity:
    return Identity(
        node_id="zero-entrance", provisioned_at="2026-09-16T00:00:00Z", boot_id=str(uuid.uuid4())
    )


def build(drivers=(), transport=None, **changes) -> tuple[Agent, FakeTransport]:
    transport = transport or FakeTransport()
    agent = Agent(
        config=config(**changes),
        identity=identity(),
        transport=transport,
        drivers=list(drivers),
        idle_seconds=0.01,
        reconnect_seconds=0.01,
        join_seconds=1.0,
    )
    return agent, transport


def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def grant(transport, agent, *, seconds=30.0, command_id="c-1", action="grant", **changes):
    message = {
        "command_id": command_id,
        "action": action,
        "node_id": "zero-entrance",
        "hub_epoch": 7,
        "capability": "events",
        "grant_id": "g-1",
        "duration_seconds": seconds,
        **changes,
    }
    transport.deliver(protocol.topic("zero-entrance", "commands"), message)


def reading(value=True, source_id="pir-1"):
    return Reading(
        source_id=source_id, kind="sensor.motion", value=value, occurred_at=datetime.now(UTC)
    )


def test_a_node_with_nothing_attached_starts_and_stops():
    agent, transport = build()
    agent.start()
    assert until(lambda: transport.on("state"))
    agent.stop()
    assert agent.stopped_cleanly
    assert transport.disconnects == 1


def test_a_node_arranges_its_own_goodbye_before_it_says_hello():
    """The will is set before connecting, and names the connection it belongs to.

    A hub that receives it after the node has already reconnected must be able to tell
    that it is about a connection that is over, not about the one it is watching.
    """
    agent, transport = build()
    agent.start()
    assert until(lambda: transport.will is not None)
    agent.stop()
    topic, payload = transport.will
    assert topic.endswith("/state")
    assert payload["online"] is False
    assert payload["connection_id"] == transport.connection_id
    assert transport.on("state")[0]["connection_id"] == transport.connection_id


def test_the_first_thing_a_node_says_is_what_it_is():
    agent, transport = build()
    agent.start()
    assert until(lambda: transport.on("state"))
    agent.stop()
    online = transport.on("state")[0]
    assert online["online"] is True
    assert online["node_id"] == "zero-entrance"
    assert online["boot_id"] == agent.identity.boot_id
    assert ("sentry/v1/nodes/zero-entrance/state", online, 1, True) in transport.published
    assert transport.on("state")[-1]["online"] is False


def test_nothing_is_published_before_the_hub_allows_it():
    agent, transport = build([Scripted("pir-1", [reading()])])
    agent.start()
    assert until(lambda: len(agent.spool) == 1)
    time.sleep(0.1)
    assert transport.on("events") == []
    grant(transport, agent)
    assert until(lambda: transport.on("events"))
    agent.stop()
    event = transport.on("events")[0]["event"]
    assert event["source_id"] == "pir-1"
    assert event["value"] is True
    assert transport.on("events")[0]["delivery"]["grant_id"] == "g-1"


def test_a_grant_that_has_run_out_stops_the_flow_without_losing_the_readings():
    agent, transport = build([Scripted("pir-1", [reading()])])
    agent.start()
    grant(transport, agent, seconds=0.05)
    assert until(lambda: transport.on("events"))
    published = len(transport.on("events"))
    time.sleep(0.2)
    agent._record(reading(False))
    time.sleep(0.2)
    assert len(transport.on("events")) == published
    assert len(agent.spool) == 1
    agent.stop()


def test_a_sensor_that_hangs_costs_its_own_source_and_nothing_else():
    agent, transport = build([Stalled("stuck-1"), Scripted("pir-1", [reading()])])
    agent.start()
    grant(transport, agent)
    assert until(lambda: transport.on("events"))
    assert until(lambda: len(transport.on("health")) >= 2)
    started = time.monotonic()
    agent.stop()
    assert time.monotonic() - started < 3
    assert not agent.stopped_cleanly
    assert any("stuck-1" in name for name in agent.unstopped)
    health = transport.on("health")[-1]
    assert health["sources"]["pir-1"]["readings"] == 1
    assert health["sources"]["stuck-1"]["readings"] == 0


def test_a_node_that_cannot_reach_the_hub_keeps_reading_up_to_its_limit():
    agent, transport = build([Motion("pir-1", interval=0.0)], event_max_count=8)
    agent.start()
    assert until(lambda: agent.spool.drops.count > 0, timeout=5)
    agent.stop()
    assert len(agent.spool) <= 8
    assert agent.spool.drops.total > 0


def test_a_message_the_hub_would_refuse_is_not_sent_at_all():
    agent, _ = build()
    agent._record(Reading("pir-1", "not a kind", True, datetime.now(UTC)))
    assert agent.refused == 1
    assert len(agent.spool) == 0


def test_a_command_repeated_is_answered_but_not_acted_on_twice():
    agent, transport = build()
    agent.start()
    grant(transport, agent, command_id="c-9")
    assert until(lambda: transport.on("acks"))
    first = agent._grant.expires_at
    grant(transport, agent, command_id="c-9", seconds=9999)
    assert until(lambda: len(transport.on("acks")) == 2)
    agent.stop()
    assert agent._grant.expires_at == first
    assert [ack["outcome"] for ack in transport.on("acks")] == ["applied", "applied"]
    assert transport.on("acks")[1]["detail"] == "already handled"


def test_a_command_for_somebody_else_is_ignored():
    agent, transport = build()
    agent.start()
    grant(transport, agent, node_id="zero-garage")
    time.sleep(0.1)
    agent.stop()
    assert transport.on("acks") == []
    assert agent._grant is None


@pytest.mark.parametrize(
    "change", [{"action": "explode"}, {"capability": "everything"}, {"duration_seconds": -1}]
)
def test_a_command_outside_what_a_node_does_is_refused(change):
    agent, transport = build()
    agent.start()
    grant(transport, agent, **change)
    time.sleep(0.1)
    agent.stop()
    assert agent._grant is None


def test_the_hub_can_take_a_grant_back():
    agent, transport = build()
    agent.start()
    grant(transport, agent)
    assert until(lambda: agent._grant is not None)
    grant(transport, agent, command_id="c-2", action="revoke")
    assert until(lambda: agent._grant is None)
    agent.stop()


def test_the_hub_can_ask_a_node_to_stop():
    agent, transport = build()
    agent.start()
    grant(transport, agent, command_id="c-3", action="stop")
    assert until(lambda: transport.on("acks"))
    assert until(lambda: agent._stop.is_set())
    agent.stop()


def test_a_renewal_that_is_not_newer_is_refused():
    agent, transport = build()
    agent.start()
    grant(transport, agent, command_id="c-1", sequence=2)
    assert until(lambda: agent._grant is not None)
    grant(transport, agent, command_id="c-2", action="renew", sequence=1, seconds=9999)
    assert until(lambda: len(transport.on("acks")) == 2)
    agent.stop()
    assert transport.on("acks")[1]["outcome"] == "failed"


def test_a_publish_that_fails_does_not_lose_the_reading():
    transport = FakeTransport(refuse_publish=True)
    agent, _ = build([Scripted("pir-1", [reading()])], transport=transport)
    agent.start()
    assert until(lambda: len(agent.spool) == 1)
    grant(transport, agent)
    time.sleep(0.3)
    agent.stop()
    assert len(agent.spool) == 1
    assert agent.published == 0


def test_health_says_what_the_queue_is_doing():
    agent, transport = build([Scripted("pir-1", [reading()])])
    agent.start()
    assert until(lambda: len(transport.on("health")) >= 2)
    agent.stop()
    health = transport.on("health")[-1]
    assert health["queue"]["events"] == 1
    assert health["queue"]["granted"] is False
    assert health["node_id"] == "zero-entrance"
    assert health["clock_status"] in {"synced", "unsynced", "unknown"}
    assert set(health["board"]) == {
        "uptime_seconds",
        "temperature_c",
        "load1",
        "memory_available_kb",
        "throttled",
    }


def test_health_is_cheap_enough_to_lose():
    agent, transport = build()
    agent.start()
    assert until(lambda: transport.on("health"))
    agent.stop()
    beats = [entry for entry in transport.published if entry[0].endswith("health")]
    assert all(qos == 0 and not retain for _, _, qos, retain in beats)

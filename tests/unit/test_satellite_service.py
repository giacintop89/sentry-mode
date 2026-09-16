"""What the hub does with what arrives, before anything is allowed to mean anything."""

import json

import pytest

from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.identity import NodeRegistry
from sentry_mode.satellites.mqtt import Message, parse_topic
from sentry_mode.satellites.service import SatelliteService, build
from sentry_mode.sources.models import SourceState
from sentry_mode.sources.registry import SourceRegistry

CONNECTION = "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21"


class FakeTransport:
    """Records what the hub sends, and never opens a socket."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []
        self.cleared: list[str] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def publish_command(self, node_id: str, payload: bytes) -> bool:
        self.commands.append((node_id, json.loads(payload)))
        return True

    def clear_retained_state(self, node_id: str) -> bool:
        self.cleared.append(node_id)
        return True


@pytest.fixture
def service(tmp_path) -> SatelliteService:
    config = SatellitesConfig(enabled=True, nodes_file=tmp_path / "nodes.json")
    nodes = NodeRegistry(config.nodes_file)
    nodes.register("zero-entrance", display_name="Ingresso", zone="entrance")
    built = SatelliteService(
        config, sources=SourceRegistry(), nodes=nodes, transport=FakeTransport()
    )
    built.sessions.broker_connected()
    return built


def approve(service) -> None:
    """Approve by fingerprint: whether the certificate matches is the broker's job."""
    if service.nodes.require("zero-entrance").status != "approved":
        service.nodes.approve("zero-entrance", pinned="a" * 64)


def message(channel: str, document: dict, node_id: str = "zero-entrance") -> Message:
    return Message(
        node_id=node_id,
        channel=channel,
        payload=json.dumps(document).encode(),
        retained=False,
        topic=f"sentry/v1/nodes/{node_id}/{channel}",
    )


def hello(connection_id: str = CONNECTION, sources=None) -> dict:
    return {
        "schema_version": 1,
        "node_id": "zero-entrance",
        "boot_id": "b1",
        "connection_id": connection_id,
        "online": True,
        "profile": "sensor-presence",
        "sources": sources if sources is not None else [{"source_id": "pir-1", "kind": "gpio"}],
    }


def event(service, *, source_id="pir-1", node_id="zero-entrance", **changes) -> Message:
    session = service.sessions.current(node_id)
    grant = session.grants["events"] if session and session.grants else None
    delivery = {
        "connection_id": session.connection_id if session else "none",
        "hub_epoch": session.hub_epoch if session else 0,
        "grant_id": grant.grant_id if grant else None,
        "queued_ms": 0,
        "replayed": False,
        "initial_state": False,
    }
    delivery.update(changes.pop("delivery", {}))
    body = {
        "schema_version": 1,
        "event": {
            "event_id": "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23",
            "node_id": changes.pop("claimed_node", node_id),
            "source_id": source_id,
            "boot_id": "b1",
            "sequence": 0,
            "kind": "sensor.motion",
            "occurred_at": "2026-09-17T09:00:00Z",
            "clock_status": "synced",
            "value": True,
            "unit": None,
            "quality": "valid",
        },
        "delivery": delivery,
    }
    return message("events", body, node_id=node_id)


def online(service, connection_id: str = CONNECTION) -> None:
    approve(service)
    service.handle(message("state", hello(connection_id)))


# -- identity -------------------------------------------------------------------


def test_a_node_that_was_never_approved_is_not_listened_to(service):
    service.handle(message("state", hello()))
    assert service.sessions.current("zero-entrance") is None
    assert service.counters.reasons["not_approved"] == 1


def test_a_node_cannot_speak_for_another_node(service):
    online(service)
    service.handle(event(service, claimed_node="zero-garage"))
    assert service.counters.reasons["identity_mismatch"] == 1
    assert service.counters.accepted == 0


def test_a_topic_is_what_says_who_sent_something():
    assert parse_topic("sentry/v1/nodes/zero-entrance/events", "sentry/v1") == (
        "zero-entrance",
        "events",
    )
    assert parse_topic("sentry/v1/nodes/zero-entrance/commands", "sentry/v1") is None
    assert parse_topic("sentry/v1/nodes//events", "sentry/v1") is None
    assert parse_topic("other/nodes/zero-entrance/events", "sentry/v1") is None


def test_a_message_claiming_a_different_name_than_its_topic_is_dropped(service):
    online(service)
    service.handle(message("health", {"node_id": "zero-garage"}))
    assert service.counters.reasons["identity_mismatch"] == 1


# -- sessions -------------------------------------------------------------------


def test_saying_hello_opens_a_session_and_earns_a_grant(service):
    online(service)
    session = service.sessions.current("zero-entrance")
    assert session.connection_id == CONNECTION
    assert session.boot_id == "b1"
    node, command = service._transport.commands[-1]
    assert node == "zero-entrance"
    assert command["action"] == "grant"
    assert command["capability"] == "events"


def test_the_sensors_a_node_declares_become_sources_the_hub_knows_about(service):
    online(service)
    record = service.sources.require("zero-entrance.pir-1")
    assert record.origin == "satellite"
    assert record.zone == "entrance"
    assert record.display_name


def test_a_sensor_of_a_kind_nobody_defined_is_not_adopted(service):
    approve(service)
    service.handle(message("state", hello(sources=[{"source_id": "x", "kind": "telepathy"}])))
    assert "zero-entrance.x" not in service.sources
    assert service.counters.reasons["unknown_source_kind"] == 1


def test_an_event_from_a_connection_that_is_over_is_refused(service):
    online(service)
    stale = event(service)
    online(service, connection_id="a-newer-one")
    service.handle(stale)
    assert service.counters.reasons["old_connection"] == 1
    assert service.counters.accepted == 0


def test_a_goodbye_that_arrives_late_does_not_bury_a_node_that_is_back(service):
    online(service, connection_id="c1")
    online(service, connection_id="c2")
    farewell = dict(hello("c1"), online=False)
    service.handle(message("state", farewell))
    assert service.sessions.current("zero-entrance") is not None
    assert service.counters.reasons["stale_goodbye"] == 1


def test_an_event_without_a_current_grant_is_refused(service):
    online(service)
    service.sessions.revoke("zero-entrance")
    message_with_dead_grant = event(service)
    service.handle(message_with_dead_grant)
    assert service.counters.reasons["no_grant"] == 1


def test_an_event_from_a_sensor_nobody_registered_is_refused(service):
    online(service)
    service.handle(event(service, source_id="pir-9"))
    assert service.counters.reasons["unregistered_source"] == 1


def test_an_event_that_passes_every_check_is_handed_on(service):
    handed = []
    service._on_event = lambda node_id, document, message: handed.append((node_id, document))
    online(service)
    service.handle(event(service))
    assert service.counters.accepted == 1
    assert handed[0][0] == "zero-entrance"


# -- revocation -----------------------------------------------------------------


def test_revoking_a_node_stops_it_mid_connection(service):
    online(service)
    live = event(service)
    service.revoke("zero-entrance", reason="sold")
    service.handle(live)
    assert service.counters.reasons["not_approved"] >= 1
    assert service.counters.accepted == 0
    assert service.sessions.current("zero-entrance") is None
    assert "zero-entrance" in service._transport.cleared
    assert service._transport.commands[-1][1]["action"] == "revoke"
    assert service.sources.require("zero-entrance.pir-1").state is SourceState.DISABLED


def test_the_hub_only_ever_sends_commands_a_node_understands(service):
    online(service)
    service.sessions.grant("zero-entrance", "events", seconds=1)
    service.renew_due_grants()
    service.revoke("zero-entrance")
    actions = {command["action"] for _, command in service._transport.commands}
    assert actions <= {"grant", "renew", "revoke", "stop"}
    for _, command in service._transport.commands:
        assert set(command) <= {
            "command_id",
            "action",
            "node_id",
            "hub_epoch",
            "capability",
            "grant_id",
            "duration_seconds",
            "sequence",
        }


# -- limits ---------------------------------------------------------------------


def test_something_too_big_is_refused_before_it_is_parsed(service):
    online(service)
    huge = Message(
        node_id="zero-entrance",
        channel="events",
        payload=b"x" * 9000,
        retained=False,
        topic="sentry/v1/nodes/zero-entrance/events",
    )
    service.handle(huge)
    assert service.counters.reasons["too_big"] == 1


def test_something_that_is_not_json_is_counted_not_raised(service):
    online(service)
    service.handle(
        Message(
            node_id="zero-entrance",
            channel="events",
            payload=b"{not json",
            retained=False,
            topic="sentry/v1/nodes/zero-entrance/events",
        )
    )
    assert service.counters.reasons["not_json"] == 1


# -- switched off ---------------------------------------------------------------


def test_a_node_with_satellites_off_builds_nothing(tmp_path):
    assert build(SatellitesConfig(), SourceRegistry()) is None


def test_a_service_that_is_off_does_not_open_a_link(tmp_path):
    config = SatellitesConfig(enabled=False, nodes_file=tmp_path / "nodes.json")
    transport = FakeTransport()
    service = SatelliteService(config, sources=SourceRegistry(), transport=transport)
    service.start()
    assert transport.started is False


def test_the_status_is_readable_before_anything_has_happened(service):
    status = service.status()
    assert status["broker"] == "connected"
    assert status["nodes"][0]["node_id"] == "zero-entrance"
    assert status["nodes"][0]["freshness"] == "never_seen"
    assert status["counters"]["accepted"] == 0

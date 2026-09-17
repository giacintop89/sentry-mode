"""What the hub does with what arrives, before anything is allowed to mean anything."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from sentry_mode.satellites.api import overview
from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.identity import NodeRegistry
from sentry_mode.satellites.mqtt import Message, parse_topic
from sentry_mode.satellites.service import SatelliteService, build
from sentry_mode.sources.models import SourceState
from sentry_mode.sources.registry import SourceRegistry

CONNECTION = "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21"
BOOT = "6f1c2d3e-4a5b-4c7d-8e9f-0a1b2c3d4e5f"


def moments_ago(seconds: float = 1) -> str:
    """Events are judged against the hub's clock, so the tests use it too."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class FakeTransport:
    """Records what the hub sends, and never opens a socket."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []
        self.cleared: list[str] = []
        self.settled: list[str] = []
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

    def settle(self, message) -> bool:
        self.settled.append(message.topic)
        return True


@pytest.fixture
def service(tmp_path) -> SatelliteService:
    config = SatellitesConfig(
        enabled=True,
        nodes_file=tmp_path / "nodes.json",
        store_path=tmp_path / "journal.sqlite3",
    )
    nodes = NodeRegistry(config.nodes_file)
    nodes.register("zero-entrance", display_name="Ingresso", zone="entrance")
    built = SatelliteService(
        config, sources=SourceRegistry(), nodes=nodes, transport=FakeTransport()
    )
    built.sessions.broker_connected()
    built.store.open()
    yield built
    built.store.close()


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
        "boot_id": BOOT,
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
            "event_id": changes.pop("event_id", "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23"),
            "node_id": changes.pop("claimed_node", node_id),
            "source_id": source_id,
            "boot_id": changes.pop("boot_id", BOOT),
            "sequence": changes.pop("sequence", 0),
            "kind": "sensor.motion",
            "occurred_at": changes.pop("occurred_at", moments_ago()),
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
    assert session.boot_id == BOOT
    node, command = service._transport.commands[-1]
    assert node == "zero-entrance"
    assert command["action"] == "grant"
    assert command["capability"] == "events"


def test_an_agent_from_another_version_is_refused_rather_than_half_believed(service):
    approve(service)
    service.handle(message("state", dict(hello(), schema_version=2)))
    assert service.sessions.current("zero-entrance") is None
    assert service.counters.reasons["unsupported_protocol"] == 1
    assert "zero-entrance.pir-1" not in service.sources
    assert service._transport.commands == []
    card = next(
        node for node in overview(service.status())["nodes"] if node["node_id"] == "zero-entrance"
    )
    assert "1 × unsupported_protocol" in card["errors"]
    assert card["online"] is False


def test_an_agent_that_comes_back_speaking_another_protocol_loses_its_session(service):
    online(service)
    service.handle(message("state", dict(hello("a-newer-one"), schema_version=None)))
    assert service.sessions.current("zero-entrance") is None
    assert service.counters.reasons["unsupported_protocol"] == 1
    service.handle(event(service))
    assert service.counters.accepted == 0


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


def test_an_event_that_passes_every_check_is_queued_for_the_rules(service):
    online(service)
    service.handle(event(service))
    assert service.counters.accepted == 1
    queued = service._queue.get_nowait()
    assert (queued.ref.id, queued.kind, queued.value) == (
        "zero-entrance.pir-1",
        "sensor.motion",
        True,
    )
    assert queued.zone == "entrance" and queued.eligible is True


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
    service.configure("zero-entrance", [{"id": "pir-1", "kind": "gpio", "line": 17}])
    service.revoke("zero-entrance")
    actions = {command["action"] for _, command in service._transport.commands}
    assert actions <= {"grant", "renew", "revoke", "stop", "configure"}
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
            "revision",
            "sources",
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


# -- the journal and the acknowledgement ----------------------------------------


def test_an_event_is_acknowledged_only_once_it_is_written_down(service):
    online(service)
    service.handle(event(service))
    assert service._transport.settled[-1].endswith("/events")
    assert service.store.counts()["journal"] == 1


def test_an_event_the_hub_could_not_write_is_left_with_the_node(service):
    online(service)
    settled = len(service._transport.settled)
    service.store.close()
    service.handle(event(service))
    assert len(service._transport.settled) == settled
    assert service.counters.reasons["store_unavailable"] == 1
    assert service.status()["journal"]["available"] is False


def test_a_deliberate_refusal_is_acknowledged_so_it_is_not_sent_for_ever(service):
    online(service)
    service.handle(event(service, source_id="pir-9"))
    assert service._transport.settled[-1].endswith("/events")


def test_a_node_over_its_budget_is_refused_and_the_others_are_not(tmp_path):
    config = SatellitesConfig(
        enabled=True,
        nodes_file=tmp_path / "nodes.json",
        store_path=tmp_path / "journal.sqlite3",
        limits={"events_per_minute": 2},
    )
    nodes = NodeRegistry(config.nodes_file)
    nodes.register("zero-entrance", display_name="Ingresso", zone="entrance")
    noisy = SatelliteService(
        config, sources=SourceRegistry(), nodes=nodes, transport=FakeTransport()
    )
    noisy.sessions.broker_connected()
    noisy.store.open()
    try:
        online(noisy)
        for sequence in range(4):
            identifier = f"0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f2{sequence}"
            noisy.handle(event(noisy, sequence=sequence, event_id=identifier))
        assert noisy.counters.accepted == 2
        assert noisy.counters.reasons["rate_limited"] == 2
        assert noisy.health.of("zero-entrance")["events"]["reasons"]["rate_limited"] == 2
    finally:
        noisy.store.close()


def test_a_full_queue_is_recorded_as_a_drop_not_as_an_acceptance(service):
    online(service)
    while not service._queue.full():
        service._queue.put_nowait(object())
    service.handle(event(service))
    assert service.counters.reasons["queue_full"] == 1
    assert service.store.recent()[0]["outcome"] == "dropped"


def test_eligible_events_reach_whoever_acts_on_them_off_the_network_thread(service):
    import threading

    delivered = threading.Event()
    seen = []

    def act(normalized):
        seen.append((normalized.ref.id, threading.current_thread().name))
        delivered.set()

    service._on_event = act
    service.start()
    try:
        online(service)
        service.handle(event(service))
        assert delivered.wait(2)
    finally:
        service.stop()
    assert seen == [("zero-entrance.pir-1", "satellite-events")]
    assert service.store.available is False


def test_a_heartbeat_is_the_current_picture_and_not_history(service):
    online(service)
    beat = {"node_id": "zero-entrance", "clock_status": "synced", "queue": {"count": 3}}
    service.handle(message("health", beat))
    service.handle(message("health", {**beat, "queue": {"count": 0}}))
    report = service.health.of("zero-entrance")
    assert (report["heartbeats"], report["queue"]) == (2, {"count": 0})
    assert service.store.counts()["journal"] == 0
    node = service.status()["nodes"][0]
    assert node["health"]["clock_status"] == "synced"


def test_a_revoked_node_leaves_no_health_behind(service):
    online(service)
    service.handle(message("health", {"node_id": "zero-entrance"}))
    service.revoke("zero-entrance", reason="lost")
    assert service.health.of("zero-entrance") is None


def test_housekeeping_keeps_a_healthy_node_heard_and_trims_by_retention(service):
    online(service)
    service.handle(event(service))
    later = service.sessions._clock() + 200  # past the renewal point, before expiry
    service.sessions._clock = lambda: later
    assert service.renew_due_grants() == 1
    assert service._transport.commands[-1][1]["action"] == "renew"
    assert service.tidy() == {"events": 0, "receipts": 0}
    service.store._clock = lambda: 10**12
    assert service.tidy() == {"events": 1, "receipts": 1}


def test_stopping_ends_every_thread_the_service_started(service):
    import threading

    service.start()
    service.stop()
    names = {thread.name for thread in threading.enumerate()}
    assert not names & {"satellite-events", "satellite-housekeeping"}


# -- configuration ----------------------------------------------------------------

PIR = [{"id": "pir-1", "kind": "gpio", "line_numbering": "bcm", "line": 17}]


def ack(command_id: str, outcome: str, detail=None) -> Message:
    return message(
        "acks",
        {
            "schema_version": 1,
            "command_id": command_id,
            "node_id": "zero-entrance",
            "outcome": outcome,
            "detail": detail,
        },
    )


def sent_configurations(service) -> list[dict]:
    return [c for _, c in service._transport.commands if c["action"] == "configure"]


def test_a_configuration_goes_to_the_current_session_and_its_answer_is_kept(service):
    online(service)
    epoch = service.sessions.current("zero-entrance").hub_epoch
    sent = service.configure("zero-entrance", PIR)
    command = sent_configurations(service)[-1]
    assert command["hub_epoch"] == epoch and command["revision"] == 1
    assert command["sources"] == PIR and command["command_id"] == sent["command_id"]
    assert service.configuration("zero-entrance")["state"] == "sent"
    service.handle(ack(sent["command_id"], "received"))
    assert service.configuration("zero-entrance")["state"] == "received"
    service.handle(ack(sent["command_id"], "applied", "revision 1"))
    service.handle(ack(sent["command_id"], "received", "already handled"))
    node = service.status()["nodes"][0]
    assert node["configuration"]["state"] == "applied"
    assert node["configuration"]["detail"] == "revision 1"


def test_a_configuration_the_node_refused_says_why_and_allows_another(service):
    online(service)
    first = service.configure("zero-entrance", PIR)
    with pytest.raises(BlockingIOError, match="has not answered configuration 1"):
        service.configure("zero-entrance", PIR)
    service.handle(
        ack(first["command_id"], "failed", "door: line 22 is busy; revision 0 is still in use")
    )
    refused = service.configuration("zero-entrance")
    assert refused["state"] == "failed" and "line 22 is busy" in refused["detail"]
    second = service.configure("zero-entrance", PIR)
    assert second["revision"] == 2


def test_an_answer_to_some_other_command_changes_nothing(service):
    online(service)
    service.configure("zero-entrance", PIR)
    service.handle(ack("not-the-one", "applied"))
    service.handle(ack(service.configuration("zero-entrance")["command_id"], "maybe"))
    assert service.configuration("zero-entrance")["state"] == "sent"


def test_the_next_revision_follows_what_the_node_says_it_runs(service):
    approve(service)
    service.handle(message("state", {**hello(), "config_revision": 7}))
    assert service.configure("zero-entrance", PIR)["revision"] == 8


def test_only_an_approved_node_that_is_online_can_be_configured(service):
    with pytest.raises(LookupError):
        service.configure("zero-garden", PIR)
    with pytest.raises(BlockingIOError, match="not approved"):
        service.configure("zero-entrance", PIR)
    approve(service)
    with pytest.raises(BlockingIOError, match="offline"):
        service.configure("zero-entrance", PIR)
    assert sent_configurations(service) == []


def test_a_command_the_broker_did_not_take_is_not_left_pending(service):
    online(service)
    service._transport.publish_command = lambda node_id, payload: False
    with pytest.raises(BlockingIOError, match="link is down"):
        service.configure("zero-entrance", PIR)
    assert service.configuration("zero-entrance")["state"] == "failed"
    service._transport.publish_command = FakeTransport().publish_command
    assert service.configure("zero-entrance", PIR)["revision"] == 2


def test_a_new_list_from_the_same_connection_keeps_the_session(service):
    online(service)
    session = service.sessions.current("zero-entrance")
    grants = len(service._transport.commands)
    changed = hello(
        sources=[
            {"source_id": "door", "kind": "gpio", "enabled": False, "options": {"line": 22}},
            {"source_id": "temp", "kind": "onewire", "options": {"device": "28-0123456789ab"}},
        ]
    )
    service.handle(message("state", {**changed, "config_revision": 3}))
    assert service.sessions.current("zero-entrance") is session
    assert len(service._transport.commands) == grants
    states = {record.ref.id: record.state for record in service.sources.all()}
    assert states["zero-entrance.pir-1"] == SourceState.DISABLED
    assert states["zero-entrance.door"] == SourceState.DISABLED
    assert states["zero-entrance.temp"] == SourceState.READY
    node = service.status()["nodes"][0]
    assert node["reported"]["config_revision"] == 3
    assert node["reported"]["sources"][1]["options"] == {"device": "28-0123456789ab"}


def test_a_revoked_node_forgets_what_it_reported_and_what_it_was_sent(service):
    online(service)
    service.configure("zero-entrance", PIR)
    service.revoke("zero-entrance")
    node = service.status()["nodes"][0]
    assert node["reported"] is None and node["configuration"] is None


def test_the_page_shows_each_source_with_its_reading_and_its_trouble(service):
    approve(service)
    declared = [
        {"source_id": "pir-1", "kind": "gpio", "options": {"line": 17}},
        {"source_id": "temp", "kind": "onewire", "options": {"device": "28-0123456789ab"}},
        {"source_id": "tag", "kind": "uvc"},
    ]
    service.handle(
        message(
            "state",
            {**hello(sources=declared), "agent_version": "0.2.0", "config_revision": 2},
        )
    )
    service.handle(
        message(
            "health",
            {
                "schema_version": 1,
                "node_id": "zero-entrance",
                "clock_status": "synced",
                "sources": {
                    "pir-1": {
                        "readings": 3,
                        "last_reading_age_seconds": 1.5,
                        "driver": "running",
                        "error": None,
                        "last": {"value": True, "unit": None, "quality": "valid"},
                    },
                    "temp": {
                        "readings": 1,
                        "last_reading_age_seconds": 40.0,
                        "driver": "running",
                        "error": "CRC mismatch",
                        "last": {"value": None, "unit": "°C", "quality": "unavailable"},
                    },
                },
            },
        )
    )
    page = overview({**service.status(), "error": None})
    assert page["enabled"] and page["drivers"] == [
        "gpio",
        "onewire",
        "bme280",
        "adc",
        "csi",
        "microphone",
        "ble",
        "dummy",
    ]
    node = page["nodes"][0]
    assert (node["display_name"], node["zone"], node["profile"]) == (
        "Ingresso",
        "entrance",
        "sensor-presence",
    )
    assert node["online"] and node["agent_version"] == "0.2.0"
    assert node["config_revision"] == 2 and node["clock_status"] == "synced"
    assert node["last_seen"] is not None
    sources = {source["name"]: source for source in node["sources"]}
    assert sources["pir-1"]["last"]["value"] is True
    assert sources["pir-1"]["options"] == {"line": 17} and sources["pir-1"]["supported"]
    assert sources["temp"]["error"] == "CRC mismatch"
    assert sources["tag"]["supported"] is False
    assert "temp: CRC mismatch" in node["errors"]
    assert overview({"enabled": False, "nodes": []})["nodes"] == []

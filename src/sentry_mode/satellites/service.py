"""Putting the satellite subsystem together, and keeping it out of the way when it is off.

Nothing in here runs unless `satellites.enabled` is true. When it does run, it is the only
place that turns a message on a topic into a node with a name: the registry says who is
allowed to speak, the session manager says whether they are speaking now, and the source
registry says which of their sensors the hub has ever heard of.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.identity import (
    NodeRegistry,
    NotApproved,
    UnknownNode,
    WrongCertificate,
)
from sentry_mode.satellites.mqtt import HubTransport, Message, TransportError, new_client_id
from sentry_mode.satellites.sessions import BrokerState, Freshness, SessionManager
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

log = logging.getLogger(__name__)

KINDS = {
    "gpio": SourceKind.SENSOR,
    "onewire": SourceKind.SENSOR,
    "bme280": SourceKind.SENSOR,
    "adc": SourceKind.SENSOR,
    "dummy": SourceKind.SENSOR,
    "csi": SourceKind.CAMERA,
    "uvc": SourceKind.CAMERA,
    "microphone": SourceKind.MICROPHONE,
    "ble": SourceKind.PRESENCE,
}


@dataclass
class Counters:
    """What happened to everything that arrived, including what was thrown away and why."""

    accepted: int = 0
    refused: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def refuse(self, reason: str) -> None:
        self.refused += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "refused": self.refused, "reasons": dict(self.reasons)}


class SatelliteService:
    """The satellite subsystem: identity, sessions and the link, wired together."""

    def __init__(
        self,
        config: SatellitesConfig,
        *,
        sources: SourceRegistry,
        nodes: NodeRegistry | None = None,
        transport: HubTransport | None = None,
        on_event: Callable[[str, dict, Message], None] | None = None,
    ) -> None:
        self.config = config
        self.sources = sources
        self.nodes = nodes or NodeRegistry(config.nodes_file)
        self.sessions = SessionManager(
            health=config.health, sessions=config.sessions, next_epoch=self.nodes.next_epoch
        )
        self.counters = Counters()
        self._on_event = on_event
        self._lock = threading.RLock()
        self._transport = transport or HubTransport(
            config.mqtt,
            on_message=self.handle,
            on_connect=self._link_up,
            on_disconnect=self._link_down,
            client_id=new_client_id(),
        )

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if not self.config.enabled:
            log.debug("satellites are switched off")
            return
        try:
            self._transport.start()
        except TransportError as error:
            self.sessions.broker_unavailable()
            log.error("the satellite link could not be started: %s", error)
            raise

    def stop(self) -> None:
        for record in self.nodes.of_status("approved"):
            self._command(record.node_id, {"action": "revoke", "capability": "events"})
        self._transport.stop()
        self.sessions.broker_stopped()

    def _link_up(self) -> None:
        self.sessions.broker_connected()
        log.info("connected to the satellite broker")

    def _link_down(self) -> None:
        self.sessions.broker_unavailable()

    # -- administration ----------------------------------------------------------

    def revoke(self, node_id: str, *, reason: str | None = None) -> None:
        """Stop believing a node immediately, whatever the broker still thinks.

        Reloading the broker's configuration does not close connections that are already
        open, so the hub takes the permission away on its own side first: the session goes,
        the grants go, the retained snapshot is erased, and anything that arrives after
        this is refused whether or not the socket is still up.
        """
        self.nodes.revoke(node_id, reason=reason)
        self.sessions.revoke(node_id)
        self.sessions.close(node_id, reason="revoked")
        self._command(node_id, {"action": "revoke", "capability": "events"})
        self._transport.clear_retained_state(node_id)
        for record in self.sources.all():
            if record.ref.node == node_id:
                self.sources.set_state(record.ref, SourceState.DISABLED)

    def approve(self, node_id: str, *, certificate: str | None = None):
        return self.nodes.approve(node_id, certificate=certificate)

    # -- messages ----------------------------------------------------------------

    def handle(self, message: Message) -> None:
        """Everything that arrives from a satellite comes through here."""
        handler = {
            "state": self._on_state,
            "health": self._on_health,
            "acks": self._on_ack,
            "events": self._on_events,
        }[message.channel]
        document = self._decode(message)
        if document is None:
            return
        handler(message, document)

    def _decode(self, message: Message) -> dict | None:
        if len(message.payload) > self.config.limits.event_max_bytes:
            self.counters.refuse("too_big")
            log.warning("a message from %s was too big to parse", message.node_id)
            return None
        if not message.payload:
            return None
        try:
            document = json.loads(message.payload)
        except ValueError:
            self.counters.refuse("not_json")
            return None
        if not isinstance(document, dict):
            self.counters.refuse("not_an_object")
            return None
        claimed = document.get("node_id")
        if claimed is not None and claimed != message.node_id:
            self.counters.refuse("identity_mismatch")
            log.warning("a message on %s claims to be from %s", message.topic, claimed)
            return None
        return document

    def _authenticated(self, node_id: str) -> bool:
        try:
            self.nodes.authenticate(node_id=node_id)
        except UnknownNode:
            self.counters.refuse("unknown_node")
        except NotApproved:
            self.counters.refuse("not_approved")
        except WrongCertificate:
            self.counters.refuse("wrong_certificate")
        else:
            return True
        return False

    def _on_state(self, message: Message, document: dict) -> None:
        """A node saying what it is. This is where a session begins and ends."""
        if not self._authenticated(message.node_id):
            return
        connection_id = document.get("connection_id")
        online = bool(document.get("online"))
        if not online:
            if self.sessions.close(message.node_id, connection_id=connection_id, reason="said so"):
                log.info("%s went offline", message.node_id)
            else:
                self.counters.refuse("stale_goodbye")
            return
        session = self.sessions.open(
            message.node_id,
            connection_id=connection_id or "unknown",
            boot_id=document.get("boot_id"),
            agent_version=document.get("agent_version"),
        )
        self._adopt_sources(message.node_id, document.get("sources", []))
        grant = self.sessions.grant(message.node_id, "events")
        self._command(message.node_id, grant.as_command())
        log.info(
            "%s is online in epoch %d with %d sources",
            message.node_id,
            session.hub_epoch,
            len(document.get("sources", [])),
        )

    def _adopt_sources(self, node_id: str, declared: list) -> None:
        """Record the sensors a node says it has. Declaring one is not permission to use it.

        The node's own configuration file is what it reads; the registry is what the hub is
        willing to believe exists. A source that turns up here and nowhere else is visible
        and unused until somebody points a rule at it.
        """
        for entry in declared if isinstance(declared, list) else []:
            if not isinstance(entry, dict):
                continue
            source_id = entry.get("source_id")
            kind = KINDS.get(entry.get("kind", ""))
            if not isinstance(source_id, str) or kind is None:
                self.counters.refuse("unknown_source_kind")
                continue
            try:
                ref = SourceRef(node=node_id, name=source_id)
            except ValueError:
                self.counters.refuse("bad_source_id")
                continue
            existing = self.sources.get(ref)
            if existing is not None:
                self.sources.set_state(ref, SourceState.READY)
                continue
            self.sources.register(
                SourceRecord(
                    ref=ref,
                    kind=kind,
                    display_name=entry.get("display_name") or f"{node_id} {source_id}",
                    origin="satellite",
                    zone=self.nodes.require(node_id).zone,
                    state=SourceState.READY,
                )
            )

    def _on_health(self, message: Message, document: dict) -> None:
        if not self._authenticated(message.node_id):
            return
        try:
            self.sessions.heartbeat(message.node_id)
        except Exception:  # noqa: BLE001 - a heartbeat without a session is not an error
            self.counters.refuse("heartbeat_without_session")

    def _on_ack(self, message: Message, document: dict) -> None:
        if not self._authenticated(message.node_id):
            return
        log.debug(
            "%s answered %s with %s",
            message.node_id,
            document.get("command_id"),
            document.get("outcome"),
        )

    def _on_events(self, message: Message, document: dict) -> None:
        """An event, checked for who sent it and whether they may. What it means is later.

        Validating the envelope, journalling it and deduplicating it is the next increment.
        What happens here is the part that cannot be deferred: an event from a node that is
        not approved, not connected, or holding no current grant never gets that far.
        """
        if not self._authenticated(message.node_id):
            return
        event = document.get("event")
        delivery = document.get("delivery")
        if not isinstance(event, dict) or not isinstance(delivery, dict):
            self.counters.refuse("not_an_envelope")
            return
        if event.get("node_id") != message.node_id:
            self.counters.refuse("identity_mismatch")
            return
        connection_id = delivery.get("connection_id")
        if not isinstance(connection_id, str) or not self.sessions.is_current(
            message.node_id, connection_id=connection_id, hub_epoch=delivery.get("hub_epoch")
        ):
            self.counters.refuse("old_connection")
            return
        grant_id = delivery.get("grant_id")
        if not isinstance(grant_id, str) or not self.sessions.grant_is_current(
            message.node_id, grant_id
        ):
            self.counters.refuse("no_grant")
            return
        source_id = event.get("source_id")
        if not isinstance(source_id, str):
            self.counters.refuse("unregistered_source")
            return
        try:
            ref = SourceRef(node=message.node_id, name=source_id)
        except ValueError:
            self.counters.refuse("bad_source_id")
            return
        if ref.id not in self.sources:
            self.counters.refuse("unregistered_source")
            return
        self.counters.accepted += 1
        if self._on_event is not None:
            self._on_event(message.node_id, document, message)

    # -- outward -----------------------------------------------------------------

    def _command(self, node_id: str, command: dict) -> bool:
        body = {"command_id": command.get("command_id"), "node_id": node_id, "hub_epoch": 0}
        body.update(command)
        body["node_id"] = node_id
        if body.get("command_id") is None:
            from uuid import uuid4

            body["command_id"] = str(uuid4())
        return self._transport.publish_command(
            node_id, json.dumps(body, separators=(",", ":")).encode("utf-8")
        )

    def renew_due_grants(self) -> int:
        """Renew what is close to lapsing. Called on a timer by whoever owns the loop."""
        renewed = 0
        for grant in self.sessions.due_for_renewal():
            fresh = self.sessions.renew(grant.node_id, grant.capability)
            if self._command(grant.node_id, fresh.as_command(action="renew")):
                renewed += 1
        return renewed

    # -- what the rest of the hub asks -------------------------------------------

    def status(self) -> dict:
        """The link, the nodes and the sensors reported apart, because they fail apart."""
        sessions = self.sessions.status()
        return {
            "enabled": self.config.enabled,
            "broker": sessions["broker"],
            "revision": self.nodes.revision,
            "counters": self.counters.as_dict(),
            "nodes": [
                {
                    "node_id": record.node_id,
                    "display_name": record.display_name,
                    "status": record.status,
                    "zone": record.zone,
                    "profile": record.profile,
                    "freshness": self.sessions.freshness(record.node_id).value,
                    "session": sessions["nodes"].get(record.node_id),
                    "sources": [
                        {
                            "source_id": source.id,
                            "kind": source.kind.value,
                            "state": source.state.value,
                        }
                        for source in self.sources.all()
                        if source.ref.node == record.node_id
                    ],
                }
                for record in self.nodes.all()
            ],
        }


def build(config: SatellitesConfig, sources: SourceRegistry, **extra) -> SatelliteService | None:
    """Make the subsystem, or nothing at all when it is switched off.

    Returning None is deliberate: a hub with satellites off holds no broker client, no node
    file and no threads, and the caller has one obvious thing to check.
    """
    if not config.enabled:
        return None
    return SatelliteService(config, sources=sources, **extra)


__all__ = ["BrokerState", "Counters", "Freshness", "SatelliteService", "build"]

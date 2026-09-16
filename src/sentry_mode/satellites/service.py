"""Putting the satellite subsystem together, and keeping it out of the way when it is off.

Nothing in here runs unless `satellites.enabled` is true. When it does run, it is the only
place that turns a message on a topic into a node with a name: the registry says who is
allowed to speak, the session manager says whether they are speaking now, and the source
registry says which of their sensors the hub has ever heard of.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from sentry_mode.satellites.config import SatellitesConfig
from sentry_mode.satellites.health import HealthBoard
from sentry_mode.satellites.identity import (
    NodeRegistry,
    NotApproved,
    UnknownNode,
    WrongCertificate,
)
from sentry_mode.satellites.ingress import EventIngress, NormalizedEvent, RateLimiter
from sentry_mode.satellites.mqtt import HubTransport, Message, TransportError, new_client_id
from sentry_mode.satellites.sessions import BrokerState, Freshness, SessionManager
from sentry_mode.satellites.store import Store, StoreUnavailable
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
        store: Store | None = None,
        on_event: Callable[[NormalizedEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.sources = sources
        self.nodes = nodes or NodeRegistry(config.nodes_file)
        self.sessions = SessionManager(
            health=config.health, sessions=config.sessions, next_epoch=self.nodes.next_epoch
        )
        self.counters = Counters()
        self.health = HealthBoard()
        self.store = store or Store(config.store_path)
        self.ingress = EventIngress(
            store=self.store,
            sources=sources,
            limits=config.limits,
            zone_of=self._zone_of,
        )
        self._limiter = RateLimiter(config.limits)
        self._queue: queue.Queue[NormalizedEvent] = queue.Queue(maxsize=config.limits.queue_depth)
        self._dispatcher: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self._stopping = threading.Event()
        self._running = threading.Event()
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
        self._open_journal()
        self._running.set()
        self._dispatcher = threading.Thread(
            target=self._dispatch, name="satellite-events", daemon=True
        )
        self._dispatcher.start()
        self._stopping.clear()
        self._keeper = threading.Thread(
            target=self._housekeeping, name="satellite-housekeeping", daemon=True
        )
        self._keeper.start()
        try:
            self._transport.start()
        except TransportError as error:
            self.sessions.broker_unavailable()
            log.error("the satellite link could not be started: %s", error)
            raise

    def _open_journal(self) -> None:
        """Open the journal, or carry on without it and say so where it can be seen.

        A hub whose disk is full or whose journal is damaged keeps its own cameras and
        rules; what it stops doing is admitting events it cannot write down, because
        acknowledging one would be claiming to have kept it.
        """
        try:
            self.store.open()
        except StoreUnavailable as error:
            log.error("the satellite journal is unusable: %s", error)
        self.health.journal(self.store.status())

    def stop(self) -> None:
        for record in self.nodes.of_status("approved"):
            self._command(record.node_id, {"action": "revoke", "capability": "events"})
        self._transport.stop()
        self.sessions.broker_stopped()
        self._running.clear()
        self._stopping.set()
        for worker in (self._dispatcher, self._keeper):
            if worker is not None:
                worker.join(timeout=5)
        self._dispatcher = self._keeper = None
        self.store.close()

    def _dispatch(self) -> None:
        """Hand eligible events to whoever acts on them, off the network thread.

        The link and the journal must not wait on a rule engine, and a rule engine must not
        be handed events straight from a broker callback, so there is a queue between them
        with a bottom to it.
        """
        while self._running.is_set():
            try:
                event = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._on_event is not None:
                    self._on_event(event)
            except Exception:  # noqa: BLE001 - one bad rule must not stop the queue
                log.exception("an event from %s could not be delivered", event.node_id)

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
        self.health.forget(node_id)
        for record in self.sources.all():
            if record.ref.node == node_id:
                self.sources.set_state(record.ref, SourceState.DISABLED)

    def approve(self, node_id: str, *, certificate: str | None = None):
        return self.nodes.approve(node_id, certificate=certificate)

    # -- messages ----------------------------------------------------------------

    def handle(self, message: Message) -> None:
        """Everything that arrives from a satellite comes through here.

        The cheap refusals come first, in the order of what they cost: who is speaking,
        how often they are speaking, how big the message is, and only then what it says.
        """
        handler = {
            "state": self._on_state,
            "health": self._on_health,
            "acks": self._on_ack,
            "events": self._on_events,
        }[message.channel]
        if not self._authenticated(message.node_id):
            self._settle(message)
            return
        if message.channel == "events":
            budget = self._limiter.allow(message.node_id)
            if budget is not None:
                self._refuse(message.node_id, "rate_limited")
                log.warning("%s is over the %s event budget", message.node_id, budget)
                # Settled deliberately: telling the broker to keep sending a message the
                # hub is refusing on purpose would make the flood worse.
                self._settle(message)
                return
        document = self._decode(message)
        if document is None:
            self._settle(message)
            return
        handler(message, document)

    def _settle(self, message: Message) -> None:
        try:
            self._transport.settle(message)
        except Exception:  # noqa: BLE001 - the link owns its own failures
            log.exception("a message from %s could not be acknowledged", message.node_id)

    def _refuse(self, node_id: str, reason: str) -> None:
        self.counters.refuse(reason)
        self.health.refused(node_id, reason)

    def _zone_of(self, node_id: str) -> str | None:
        record = self.nodes.get(node_id)
        return record.zone if record is not None else None

    def _decode(self, message: Message) -> dict | None:
        if len(message.payload) > self.config.limits.event_max_bytes:
            self._refuse(message.node_id, "too_big")
            log.warning("a message from %s was too big to parse", message.node_id)
            return None
        if not message.payload:
            return None
        try:
            document = json.loads(message.payload)
        except ValueError:
            self._refuse(message.node_id, "not_json")
            return None
        if not isinstance(document, dict):
            self._refuse(message.node_id, "not_an_object")
            return None
        claimed = document.get("node_id")
        if claimed is not None and claimed != message.node_id:
            self._refuse(message.node_id, "identity_mismatch")
            log.warning("a message on %s claims to be from %s", message.topic, claimed)
            return None
        return document

    def _authenticated(self, node_id: str) -> bool:
        """Counted in the total only: a name nobody approved does not get a health entry.

        The broker's access control is what keeps a stranger off these topics at all, and a
        name that gets past it anyway must not be able to make the hub keep a record per
        name it invents.
        """
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
        connection_id = document.get("connection_id")
        online = bool(document.get("online"))
        if not online:
            if self.sessions.close(message.node_id, connection_id=connection_id, reason="said so"):
                log.info("%s went offline", message.node_id)
            else:
                self._refuse(message.node_id, "stale_goodbye")
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
                self._refuse(node_id, "unknown_source_kind")
                continue
            try:
                ref = SourceRef(node=node_id, name=source_id)
            except ValueError:
                self._refuse(node_id, "bad_source_id")
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
        """A heartbeat replaces the one before it. None of them reaches the journal."""
        self.health.heartbeat(message.node_id, document)
        try:
            self.sessions.heartbeat(message.node_id)
        except Exception:  # noqa: BLE001 - a heartbeat without a session is not an error
            self._refuse(message.node_id, "heartbeat_without_session")

    def _on_ack(self, message: Message, document: dict) -> None:
        log.debug(
            "%s answered %s with %s",
            message.node_id,
            document.get("command_id"),
            document.get("outcome"),
        )

    def _on_events(self, message: Message, document: dict) -> None:
        """An event: who sent it, whether they may, what it is, and then the journal.

        The order matters. The checks here are about the sender and cost nothing, so they
        come before the envelope is validated; the journal is written before the message is
        acknowledged; and the rule engine hears about it last, through a queue, so that
        neither the link nor the writer waits on it.
        """
        delivery = document.get("delivery")
        if not isinstance(document.get("event"), dict) or not isinstance(delivery, dict):
            self._refuse(message.node_id, "not_an_envelope")
            self._settle(message)
            return
        connection_id = delivery.get("connection_id")
        if not isinstance(connection_id, str) or not self.sessions.is_current(
            message.node_id, connection_id=connection_id, hub_epoch=delivery.get("hub_epoch")
        ):
            self._refuse(message.node_id, "old_connection")
            self._settle(message)
            return
        grant_id = delivery.get("grant_id")
        if not isinstance(grant_id, str) or not self.sessions.grant_is_current(
            message.node_id, grant_id
        ):
            self._refuse(message.node_id, "no_grant")
            self._settle(message)
            return
        receipt = self.ingress.accept(message.node_id, document, retained=message.retained)
        if receipt.settle:
            self._settle(message)
        else:
            # Nothing was written, so nothing is acknowledged and the node keeps its copy.
            self.health.journal(self.store.status())
        if receipt.reason is not None:
            self._refuse(message.node_id, receipt.reason)
            return
        self.counters.accepted += 1
        self.health.accepted(message.node_id)
        if receipt.event is None:
            return
        try:
            self._queue.put_nowait(receipt.event)
        except queue.Full:
            # A full queue is an outcome, not an acceptance: the event stays in the journal
            # marked as dropped rather than being counted as one the hub acted on.
            self._refuse(message.node_id, "queue_full")
            log.warning("the event queue is full; an event from %s was dropped", message.node_id)
            try:
                self.store.outcome(message.node_id, receipt.event.event_id, "dropped")
            except StoreUnavailable:  # pragma: no cover - the write above had just worked
                pass

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

    def _housekeeping(self, *, every: float = 5.0) -> None:
        """Renew grants before they lapse, and trim the journal now and then.

        Without the renewals a node's permission to report runs out a few minutes after it
        connects, so this is not optional tidying: it is what keeps a healthy node heard.
        """
        tidied_at = float("-inf")
        while not self._stopping.wait(every):
            try:
                self.renew_due_grants()
                now = time.monotonic()
                if now - tidied_at >= 3600:
                    tidied_at = now
                    self.tidy()
            except Exception:  # noqa: BLE001 - housekeeping must outlive one bad round
                log.exception("satellite housekeeping failed")

    def tidy(self) -> dict:
        """Forget history and receipts past their retention, each by its own clock."""
        limits = self.config.limits
        if not self.store.available:
            return {"events": 0, "receipts": 0}
        removed = {
            "events": self.store.forget_events(older_than_seconds=limits.journal_days * 86400),
            "receipts": self.store.forget_receipts(
                older_than_seconds=limits.dedup_days * 86400
            ),
        }
        self.health.journal(self.store.status())
        return removed

    def renew_due_grants(self) -> int:
        """Renew what is close to lapsing. The housekeeping thread calls this."""
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
            "journal": self.store.status(),
            "nodes": [
                {
                    "node_id": record.node_id,
                    "display_name": record.display_name,
                    "status": record.status,
                    "zone": record.zone,
                    "profile": record.profile,
                    "freshness": self.sessions.freshness(record.node_id).value,
                    "session": sessions["nodes"].get(record.node_id),
                    "health": self.health.of(record.node_id),
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


__all__ = [
    "BrokerState",
    "Counters",
    "Freshness",
    "NormalizedEvent",
    "SatelliteService",
    "build",
]

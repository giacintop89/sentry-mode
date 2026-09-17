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
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from sentry_mode.satellites import platforms
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
from sentry_mode.satellites.protocol import SCHEMA_VERSION
from sentry_mode.satellites.sessions import BrokerState, Freshness, SessionManager
from sentry_mode.satellites.store import Store, StoreUnavailable
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

if TYPE_CHECKING:
    from sentry_mode.audio.remote import MicrophoneTable
    from sentry_mode.config import DetectionConfig
    from sentry_mode.sources.manager import SourceManager
    from sentry_mode.vision.media_gateway import MediaGateway
    from sentry_mode.vision.scheduler import InferenceScheduler

log = logging.getLogger(__name__)

CONFIGURE_PATIENCE_SECONDS = 30.0
"""How long a configuration may go unanswered before another one is allowed."""

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
    "board": SourceKind.SENSOR,
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
        cameras: SourceManager | None = None,
        inference: InferenceScheduler | None = None,
        detection: DetectionConfig | None = None,
        gateway: MediaGateway | None = None,
        microphones: MicrophoneTable | None = None,
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
        self._reported: dict[str, dict] = {}
        self._configurations: dict[str, dict] = {}
        # Video: satellite cameras become cameras of this hub, driven by leases.
        self.cameras = cameras
        self.inference = inference
        self.detection = detection
        self.gateway = gateway
        self.media_error: str | None = None
        # Sound: satellite microphones, heard while somebody listens or records.
        self.microphones = microphones
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
        self._open_media()
        try:
            self._transport.start()
        except TransportError as error:
            self.sessions.broker_unavailable()
            log.error("the satellite link could not be started: %s", error)
            raise

    def _open_media(self) -> None:
        """Open the media port, or report why not and carry on with events only."""
        media = self.config.media
        wanted = (self.cameras is not None and self.inference is not None) or (
            self.microphones is not None
        )
        if not media.enabled or not wanted:
            return
        try:
            if self.gateway is None:
                from sentry_mode.vision.media_gateway import MediaGateway, server_context

                self.gateway = MediaGateway(media, server_context(self.config.mqtt), self.nodes)
            self.gateway.start()
        except Exception as error:  # noqa: BLE001 - cameras on this hub do not depend on it
            self.gateway = None
            self.media_error = str(error)
            log.error("satellite video and sound are unavailable: %s", error)

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
        if self.gateway is not None:
            self.gateway.stop()
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
        self._end_video(node_id, "the node was revoked")
        self._command(node_id, {"action": "revoke", "capability": "events"})
        self._transport.clear_retained_state(node_id)
        self.health.forget(node_id)
        with self._lock:
            self._reported.pop(node_id, None)
            self._configurations.pop(node_id, None)
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
        if not self._speaks_our_protocol(message.node_id, document):
            return
        connection_id = document.get("connection_id")
        online = bool(document.get("online"))
        if online:
            self._remember_report(message.node_id, document)
        if not online:
            if self.sessions.close(message.node_id, connection_id=connection_id, reason="said so"):
                log.info("%s went offline", message.node_id)
                self._end_video(message.node_id, "the node went offline")
            else:
                self._refuse(message.node_id, "stale_goodbye")
            return
        current = self.sessions.current(message.node_id)
        if (
            current is not None
            and current.connection_id == connection_id
            and current.boot_id == document.get("boot_id")
        ):
            # The same connection saying what it is now, after a configuration change.
            # The session and its grant carry on; only the list of sources moves.
            self._adopt_sources(message.node_id, document.get("sources", []))
            return
        # A new session: whatever was streaming belonged to the old one.
        self._end_video(message.node_id, "the node started a new session")
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

    def _speaks_our_protocol(self, node_id: str, document: dict) -> bool:
        """Whether this hub and that agent are talking about the same wire at all.

        An agent from another version is not a node with a small problem: nothing it says
        can be read safely, so no session opens, no grant is issued, and whatever it was
        streaming ends. It is refused loudly rather than left green while its events are
        quietly dropped one at a time.
        """
        spoken = document.get("schema_version")
        if spoken == SCHEMA_VERSION:
            return True
        self._refuse(node_id, "unsupported_protocol")
        log.warning(
            "%s speaks protocol %r; this hub speaks %d, so it is not being listened to",
            node_id,
            spoken,
            SCHEMA_VERSION,
        )
        if self.sessions.close(node_id, connection_id=None, reason="unsupported protocol"):
            self._end_video(node_id, "the node speaks a protocol this hub does not")
        return False

    def _remember_report(self, node_id: str, document: dict) -> None:
        declared = document.get("sources")
        revision = document.get("config_revision")
        with self._lock:
            self._reported[node_id] = {
                "profile": document.get("profile"),
                "agent_version": document.get("agent_version"),
                "config_revision": revision
                if isinstance(revision, int) and not isinstance(revision, bool)
                else None,
                "sources": [
                    {
                        "source_id": entry.get("source_id"),
                        "kind": entry.get("kind"),
                        "enabled": entry.get("enabled", True) is not False,
                        "options": entry.get("options")
                        if isinstance(entry.get("options"), dict)
                        else {},
                    }
                    for entry in (declared if isinstance(declared, list) else [])[:64]
                    if isinstance(entry, dict)
                ],
            }

    def _adopt_sources(self, node_id: str, declared: list) -> None:
        """Record the sensors a node says it has. Declaring one is not permission to use it.

        The node's own configuration file is what it reads; the registry is what the hub is
        willing to believe exists. A source that turns up here and nowhere else is visible
        and unused until somebody points a rule at it.
        """
        named: set[SourceRef] = set()
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
            named.add(ref)
            if entry.get("kind") == "csi" and entry.get("enabled") is not False:
                self._adopt_camera(node_id, source_id, entry)
            if entry.get("kind") == "microphone" and entry.get("enabled") is not False:
                self._adopt_microphone(node_id, source_id, entry)
            state = SourceState.DISABLED if entry.get("enabled") is False else SourceState.READY
            existing = self.sources.get(ref)
            if existing is not None:
                self.sources.set_state(ref, state)
                continue
            self.sources.register(
                SourceRecord(
                    ref=ref,
                    kind=kind,
                    display_name=entry.get("display_name") or f"{node_id} {source_id}",
                    origin="satellite",
                    zone=self.nodes.require(node_id).zone,
                    state=state,
                )
            )
        # A source the node no longer declares stays known, so rules that name it still
        # read as they were written, but nothing may count on it.
        for record in self.sources.all():
            if record.ref.node == node_id and record.ref not in named:
                self.sources.set_state(record.ref, SourceState.DISABLED)

    def _adopt_camera(self, node_id: str, source_id: str, entry: dict) -> None:
        """Make a satellite camera one of this hub's cameras, the first time it is declared.

        It is registered with the camera table and does nothing until something leases it.
        Its size is fixed when it is first seen; a node that changes it later is streamed
        at the size the hub decodes, and scaled.
        """
        if self.cameras is None or self.inference is None or self.detection is None:
            return
        ref = f"{node_id}.{source_id}"
        if ref in self.cameras:
            return
        from sentry_mode.vision.remote import RemoteCamera

        options = entry.get("options")
        if not isinstance(options, dict):
            options = {}
        camera = RemoteCamera(
            node_id,
            source_id,
            options,
            link=self,
            scheduler=self.inference,
            detection=self.detection,
            media=self.config.media,
            display_name=entry.get("display_name") or None,
        )
        try:
            self.cameras.add(camera, register=False)
        except ValueError:
            return
        log.info("%s is a camera of this hub now", ref)

    def _adopt_microphone(self, node_id: str, source_id: str, entry: dict) -> None:
        """Make a satellite microphone one this hub can hear. It sends nothing until asked."""
        if self.microphones is None:
            return
        ref = f"{node_id}.{source_id}"
        if ref in self.microphones:
            return
        from sentry_mode.audio.remote import RemoteMicrophone

        options = entry.get("options")
        microphone = RemoteMicrophone(
            node_id,
            source_id,
            options if isinstance(options, dict) else {},
            link=self,
            media=self.config.media,
            display_name=entry.get("display_name") or None,
        )
        try:
            self.microphones.add(microphone)
        except ValueError:
            return
        log.info("%s is a microphone this hub can hear now", ref)

    # -- video and sound ---------------------------------------------------------

    def start_video(
        self,
        node_id: str,
        source_id: str,
        connect: Callable[[], Any],
        on_end: Callable[[str, bool], None],
    ) -> str:
        """Ask a node for one camera's video. The stream id comes back; the token does not."""
        return self._start_media("video", node_id, source_id, connect, on_end)

    def start_audio(
        self,
        node_id: str,
        source_id: str,
        connect: Callable[[], Any],
        on_end: Callable[[str, bool], None],
    ) -> str:
        """Ask a node for one microphone's sound, the same way."""
        return self._start_media("audio", node_id, source_id, connect, on_end)

    def renew_video(self, node_id: str, stream_id: str) -> bool:
        return self._renew_media("video", node_id, stream_id)

    def renew_audio(self, node_id: str, stream_id: str) -> bool:
        return self._renew_media("audio", node_id, stream_id)

    def stop_video(self, node_id: str, stream_id: str) -> None:
        self._stop_media("video", node_id, stream_id)

    def stop_audio(self, node_id: str, stream_id: str) -> None:
        self._stop_media("audio", node_id, stream_id)

    def _start_media(
        self,
        kind: str,
        node_id: str,
        source_id: str,
        connect: Callable[[], Any],
        on_end: Callable[[str, bool], None],
    ) -> str:
        if self.gateway is None:
            raise BlockingIOError(self.media_error or f"satellite {kind} is switched off")
        record = self.nodes.get(node_id)
        if record is None or record.status != "approved":
            raise BlockingIOError(f"{node_id} is not approved")
        session = self.sessions.current(node_id)
        if session is None:
            raise BlockingIOError(f"{node_id} is offline")
        if self.sessions.granted(node_id) is None:
            raise BlockingIOError(f"{node_id} has no current grant")
        ticket = self.gateway.open(node_id, source_id, connect, on_end=on_end, kind=kind)
        sent = self._command(
            node_id,
            {
                "action": f"{kind}_start",
                "hub_epoch": session.hub_epoch,
                "stream_id": ticket.stream_id,
                "source_id": source_id,
                "port": ticket.port,
                "token": ticket.token,
                "duration_seconds": self.config.media.stream_seconds,
            },
        )
        if not sent:
            self.gateway.close(ticket.stream_id, "the command could not be sent")
            raise BlockingIOError("the satellite link is down")
        log.info("asked %s for %s from %s as %s", node_id, kind, source_id, ticket.stream_id)
        return ticket.stream_id

    def _renew_media(self, kind: str, node_id: str, stream_id: str) -> bool:
        session = self.sessions.current(node_id)
        if self.gateway is None or session is None:
            return False
        if not self.gateway.renew(stream_id):
            return False
        return self._command(
            node_id,
            {
                "action": f"{kind}_renew",
                "hub_epoch": session.hub_epoch,
                "stream_id": stream_id,
                "duration_seconds": self.config.media.stream_seconds,
            },
        )

    def _stop_media(self, kind: str, node_id: str, stream_id: str) -> None:
        if self.gateway is not None:
            self.gateway.close(stream_id)
        session = self.sessions.current(node_id)
        if session is not None:
            self._command(
                node_id,
                {"action": f"{kind}_stop", "hub_epoch": session.hub_epoch, "stream_id": stream_id},
            )

    def _end_video(self, node_id: str, reason: str) -> None:
        """A publication already open ends too, not only the next one."""
        if self.gateway is not None:
            self.gateway.close_node(node_id, reason)

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
        outcome = document.get("outcome")
        detail = document.get("detail")
        with self._lock:
            pending = self._configurations.get(message.node_id)
            if pending is None or pending["command_id"] != document.get("command_id"):
                return
            if outcome not in ("received", "applied", "failed"):
                return
            if outcome == "received" and pending["state"] != "sent":
                return  # a late copy of the first answer says nothing new
            pending["state"] = outcome
            pending["detail"] = detail if isinstance(detail, str) else None
            pending["answered_at"] = time.time()
        if outcome == "failed":
            log.warning("%s refused configuration: %s", message.node_id, detail)
        elif outcome == "applied":
            log.info("%s applied configuration %s", message.node_id, pending["revision"])

    # -- configuration -----------------------------------------------------------

    def configure(self, node_id: str, sources: list[dict]) -> dict:
        """Send a node a new list of sources. The answer arrives later, on its acks topic.

        Refused here, before anything is sent: a node that is not approved or not online,
        and a node that has not answered the previous request yet.
        """
        record = self.nodes.get(node_id)
        if record is None:
            raise LookupError(f"{node_id} is not a registered satellite")
        if record.status != "approved":
            raise BlockingIOError(f"{node_id} is not approved")
        session = self.sessions.current(node_id)
        if session is None:
            raise BlockingIOError(f"{node_id} is offline; it can be configured once it is back")
        # What this kind of machine can be asked for at all. The node checks everything
        # again and has the last word; this is here so that a camera sent to a
        # microcontroller is an error on the page rather than a round trip that can only
        # come back `failed`.
        wrong = platforms.check_configuration(platforms.platform(record.platform), sources)
        if wrong:
            raise ValueError("; ".join(wrong))
        now = time.time()
        with self._lock:
            pending = self._configurations.get(node_id)
            if (
                pending is not None
                and pending["state"] in ("sent", "received")
                and now - pending["sent_at"] < CONFIGURE_PATIENCE_SECONDS
            ):
                raise BlockingIOError(
                    f"{node_id} has not answered configuration {pending['revision']} yet"
                )
            reported = (self._reported.get(node_id) or {}).get("config_revision") or 0
            revision = max(reported, pending["revision"] if pending else 0) + 1
            command_id = str(uuid.uuid4())
            entry = {
                "command_id": command_id,
                "revision": revision,
                "state": "sent",
                "detail": None,
                "sent_at": now,
                "answered_at": None,
            }
            self._configurations[node_id] = entry
        sent = self._command(
            node_id,
            {
                "command_id": command_id,
                "action": "configure",
                "hub_epoch": session.hub_epoch,
                "revision": revision,
                "sources": sources,
            },
        )
        if not sent:
            with self._lock:
                entry["state"] = "failed"
                entry["detail"] = "the broker did not take the command"
            raise BlockingIOError("The satellite link is down; nothing was sent.")
        log.info("sent configuration %d to %s", revision, node_id)
        return dict(entry)

    def configuration(self, node_id: str) -> dict | None:
        with self._lock:
            entry = self._configurations.get(node_id)
            return dict(entry) if entry else None

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
            "receipts": self.store.forget_receipts(older_than_seconds=limits.dedup_days * 86400),
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
            "media": {
                "enabled": self.config.media.enabled,
                "error": self.media_error,
                "gateway": None if self.gateway is None else self.gateway.status(),
            },
            "nodes": [
                {
                    "node_id": record.node_id,
                    "display_name": record.display_name,
                    "status": record.status,
                    "zone": record.zone,
                    "profile": record.profile,
                    "platform": record.platform,
                    "freshness": self.sessions.freshness(record.node_id).value,
                    "session": sessions["nodes"].get(record.node_id),
                    "health": self.health.of(record.node_id),
                    "reported": self._reported.get(record.node_id),
                    "configuration": self.configuration(record.node_id),
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

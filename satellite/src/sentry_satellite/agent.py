"""The agent: read what is here, tell the hub, do as the hub says, stop when asked.

Three kinds of thread, deliberately kept apart. Each driver reads on its own, so one that
hangs costs its own source and nothing else. One thread talks to the network. One reports
health on a fixed beat, whatever the other two are doing. Nothing decides anything about
the house: the agent is a peripheral of the protocol.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Callable, Iterable

from sentry_satellite import __version__, commands, health, protocol
from sentry_satellite.config import Config
from sentry_satellite.identity import Identity
from sentry_satellite.mqtt import Transport, TransportError
from sentry_satellite.sensors import Driver, Reading
from sentry_satellite.spool import Spool

log = logging.getLogger(__name__)

GRANT_REQUIRED = "events"
"""Before the hub grants it, the agent may say it is alive, and nothing more."""


@dataclass
class Grant:
    """Permission to publish one capability, for a while, in one epoch of the hub."""

    grant_id: str
    capability: str
    hub_epoch: int
    expires_at: float
    sequence: int = 0

    def valid(self, now: float) -> bool:
        return now < self.expires_at


@dataclass
class SourceState:
    readings: int = 0
    last_reading_at: float | None = None
    driver_alive: bool = True

    def as_dict(self, now: float) -> dict:
        age = None if self.last_reading_at is None else round(now - self.last_reading_at, 1)
        return {
            "readings": self.readings,
            "last_reading_age_seconds": age,
            "driver": "running" if self.driver_alive else "finished",
        }


@dataclass
class Agent:
    config: Config
    identity: Identity
    transport: Transport
    drivers: Iterable[Driver]
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    reconnect_seconds: float = 2.0
    idle_seconds: float = 0.05
    join_seconds: float = 2.0

    _stop: Event = field(default_factory=Event, init=False, repr=False)
    _threads: list[Thread] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _grant: Grant | None = field(default=None, init=False, repr=False)
    _seen: commands.Seen = field(default_factory=commands.Seen, init=False, repr=False)
    _states: dict[str, SourceState] = field(default_factory=dict, init=False, repr=False)
    published: int = field(default=0, init=False)
    refused: int = field(default=0, init=False)
    unstopped: list[str] = field(default_factory=list, init=False)
    clock_status: str = field(default="unknown", init=False)

    def __post_init__(self) -> None:
        self.drivers = list(self.drivers)
        self.started_at = self.clock()
        self.spool = Spool(
            max_count=self.config.limits.event_max_count,
            max_bytes=self.config.limits.event_max_bytes,
            max_age_seconds=self.config.limits.event_max_age_seconds,
            clock=self.clock,
        )
        self.events = protocol.Events(self.identity.node_id, self.identity.boot_id)
        self._states = {driver.source_id: SourceState() for driver in self.drivers}

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        log.info("starting as %s", self.identity.summary)
        self.refresh_clock_status()
        self.transport.subscribe(self._topic("commands"), self._on_command)
        self._spawn("network", self._network)
        self._spawn("health", self._health)
        for driver in self.drivers:
            self._spawn(f"driver:{driver.source_id}", self._drive, driver)

    def stop(self) -> None:
        """Ask everything to stop, and report what did not.

        A driver stuck in a blocking read cannot be interrupted from outside, so its thread
        is a daemon and the process leaves without it. Saying so is the point: silence
        would make an unstoppable sensor look like a clean shutdown.
        """
        self._stop.set()
        for driver in self.drivers:
            try:
                driver.stop()
            except Exception:  # noqa: BLE001 - one rude driver must not block the rest
                log.exception("%s raised while stopping", driver.source_id)
        for thread in self._threads:
            thread.join(timeout=self.join_seconds)
            if thread.is_alive():
                self.unstopped.append(thread.name)
                log.warning("%s did not stop", thread.name)
        self._publish_state(online=False)
        self.transport.disconnect()
        log.info("stopped; %d published, %d refused", self.published, self.refused)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.stop()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopped_cleanly(self) -> bool:
        return not self.unstopped

    # -- threads -----------------------------------------------------------------

    def _spawn(self, name: str, target: Callable, *args) -> None:
        thread = Thread(target=self._guard, args=(name, target, *args), name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _guard(self, name: str, target: Callable, *args) -> None:
        try:
            target(*args)
        except Exception:  # noqa: BLE001 - a thread that dies quietly is a sensor that lies
            log.exception("%s stopped unexpectedly", name)

    def _drive(self, driver: Driver) -> None:
        state = self._states[driver.source_id]
        try:
            for reading in driver.read():
                if self._stop.is_set():
                    break
                self._record(reading)
                state.readings += 1
                state.last_reading_at = self.clock()
        finally:
            state.driver_alive = False

    def _network(self) -> None:
        while not self._stop.is_set():
            if not self.transport.connected:
                self._connect()
                continue
            held = self.spool.take(8) if self._granted() else []
            if not held:
                self._stop.wait(self.idle_seconds)
                continue
            for item in held:
                self._send(item)

    def _health(self) -> None:
        beat = self.config.limits.heartbeat_seconds
        while not self._stop.wait(beat):
            self.refresh_clock_status()
            self._publish_health()

    # -- work --------------------------------------------------------------------

    def _connect(self) -> None:
        connection_id = str(uuid.uuid4())
        self.transport.set_will(
            self._topic("state"), self._state_payload(online=False, connection_id=connection_id)
        )
        try:
            self.transport.connect(connection_id)
        except TransportError as error:
            log.warning("no link to the hub: %s", error)
            self._stop.wait(self.reconnect_seconds)
            return
        self._publish_state(online=True)
        self._publish_health()

    def _record(self, reading: Reading) -> None:
        """Turn a reading into an event and put it in the queue. Never blocks on the link."""
        try:
            event = self.events.event(
                reading.source_id,
                reading.kind,
                reading.value,
                occurred_at=reading.occurred_at,
                unit=reading.unit,
                quality=reading.quality,
                clock_status=self.clock_status,
            )
        except protocol.ProtocolError as error:
            self.refused += 1
            log.warning("%s produced something unsendable: %s", reading.source_id, error)
            return
        size = len(json.dumps(event, separators=(",", ":")).encode("utf-8"))
        self.spool.put(self._topic("events"), event, size)

    def _send(self, held) -> None:
        delivery = self._delivery(queued_ms=self.spool.queued_ms(held))
        if delivery is None:
            self.spool.requeue(held)
            return
        try:
            payload = protocol.encode(protocol.envelope(held.item, delivery))
        except protocol.ProtocolError as error:
            self.refused += 1
            log.warning("dropping an event the hub would refuse: %s", error)
            return
        if self.transport.publish(held.topic, payload, qos=1):
            self.published += 1
        else:
            self.spool.requeue(held)
            self._stop.wait(self.idle_seconds)

    def _state_payload(self, *, online: bool, connection_id: str) -> bytes:
        """The retained snapshot: what this node is, and whether it is here.

        Retained, because a hub that starts later still needs to know the node exists. A
        snapshot, never a pulse: an event that happened belongs on the events topic, where
        nobody will replay it as if it had just happened. It names its connection, so that
        a goodbye which took the long way round cannot bury a node that is already back.
        """
        state = {
            "schema_version": 1,
            "node_id": self.identity.node_id,
            "boot_id": self.identity.boot_id,
            "connection_id": connection_id,
            "agent_version": __version__,
            "profile": self.config.profile,
            "online": online,
            "sources": [
                {"source_id": source.id, "kind": source.kind} for source in self.config.sources
            ],
        }
        return json.dumps(state, separators=(",", ":")).encode("utf-8")

    def _publish_state(self, *, online: bool) -> None:
        self.transport.publish(
            self._topic("state"),
            self._state_payload(online=online, connection_id=self.transport.connection_id),
            qos=1,
            retain=True,
        )

    def _publish_health(self) -> None:
        now = self.clock()
        payload = health.payload(
            node_id=self.identity.node_id,
            boot_id=self.identity.boot_id,
            started_at=self.started_at,
            clock=self.clock_status,
            queue={
                "events": len(self.spool),
                "bytes": self.spool.nbytes,
                "published": self.published,
                "refused": self.refused,
                "drops": self.spool.drops.as_dict(),
                "granted": self._granted(),
            },
            sources={source_id: state.as_dict(now) for source_id, state in self._states.items()},
            now=now,
        )
        self._publish_json(self._topic("health"), payload, qos=0, retain=False)

    def _publish_json(self, topic: str, message: dict, *, qos: int, retain: bool) -> None:
        try:
            payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except ValueError:
            log.warning("could not encode a message for %s", topic)
            return
        self.transport.publish(topic, payload, qos=qos, retain=retain)

    # -- commands ----------------------------------------------------------------

    def _on_command(self, topic: str, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            log.warning("a command arrived that is not JSON")
            return
        try:
            command = commands.parse(message, node_id=self.identity.node_id)
        except commands.CommandError as error:
            log.warning("refusing a command: %s", error)
            return
        previous = self._seen.outcome(command.command_id)
        if previous is not None:
            self._publish_json(
                self._topic("acks"),
                commands.ack(command, previous, detail="already handled"),
                qos=1,
                retain=False,
            )
            return
        outcome, detail = self._apply(command)
        self._seen.remember(command.command_id, outcome)
        self._publish_json(
            self._topic("acks"), commands.ack(command, outcome, detail=detail), qos=1, retain=False
        )

    def _apply(self, command: commands.Command) -> tuple[commands.Outcome, str | None]:
        if command.action == "stop":
            self.request_stop()
            return "applied", "stopping"
        if command.action == "revoke":
            with self._lock:
                self._grant = None
            return "applied", None
        if command.action in {"grant", "renew"}:
            if command.grant_id is None or command.capability is None:
                return "failed", "a grant names a capability and has an id"
            if command.duration_seconds <= 0:
                return "failed", "a grant that has already expired is not a grant"
            with self._lock:
                held = self._grant
                if command.action == "renew":
                    if held is None or held.grant_id != command.grant_id:
                        return "failed", "there is no such grant to renew"
                    if command.sequence <= held.sequence:
                        return "failed", "a renewal that is not newer than the grant it renews"
                self._grant = Grant(
                    grant_id=command.grant_id,
                    capability=command.capability,
                    hub_epoch=command.hub_epoch,
                    expires_at=self.clock() + command.duration_seconds,
                    sequence=command.sequence,
                )
            return "applied", None
        return "failed", "unknown action"

    # -- small things ------------------------------------------------------------

    def _topic(self, channel: str) -> str:
        return protocol.topic(self.identity.node_id, channel)

    def _granted(self) -> bool:
        with self._lock:
            grant = self._grant
        if grant is None or grant.capability != GRANT_REQUIRED:
            return False
        return grant.valid(self.clock())

    def _delivery(self, *, queued_ms: int) -> protocol.Delivery | None:
        with self._lock:
            grant = self._grant
        if grant is None or not grant.valid(self.clock()):
            return None
        return protocol.Delivery(
            connection_id=self.transport.connection_id,
            hub_epoch=grant.hub_epoch,
            grant_id=grant.grant_id,
            queued_ms=queued_ms,
            replayed=queued_ms > 0,
        )

    def refresh_clock_status(self) -> str:
        """Ask the operating system on the health beat, never once per reading.

        Whether this board's clock is trustworthy changes on the scale of minutes, and
        asking costs a subprocess. The hub gets the answer with every heartbeat and makes
        up its own mind regardless.
        """
        self.clock_status = health.clock_status()
        return self.clock_status

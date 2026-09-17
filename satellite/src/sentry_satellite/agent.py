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
from typing import Any, Callable, Iterable, Protocol

from sentry_satellite import __version__, commands, health, protocol, remote
from sentry_satellite.audio.publisher import AudioPublisher
from sentry_satellite.camera.publisher import Connection, Publisher, Stream
from sentry_satellite.config import Config
from sentry_satellite.identity import Identity
from sentry_satellite.mqtt import Transport, TransportError
from sentry_satellite.sensors import Driver, Reading, baseline
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


class Rebuilder(Protocol):
    """What remote configuration needs from `drivers.Builder`, without importing hardware."""

    kinds: tuple[str, ...]

    def check(self, config: Config) -> None: ...

    def __call__(self, config: Config) -> list[Driver]: ...

    def release(self) -> None: ...


@dataclass
class SourceState:
    readings: int = 0
    last_reading_at: float | None = None
    driver_alive: bool = True
    retired: bool = False
    last: Reading | None = None
    driver: Any = field(default=None, repr=False)

    def as_dict(self, now: float) -> dict:
        age = None if self.last_reading_at is None else round(now - self.last_reading_at, 1)
        last = self.last
        return {
            "readings": self.readings,
            "last_reading_age_seconds": age,
            "driver": "running" if self.driver_alive else "finished",
            "error": getattr(self.driver, "error", None),
            "last": None
            if last is None
            else {"value": last.value, "unit": last.unit, "quality": last.quality},
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
    link_seconds: float = 10.0
    idle_seconds: float = 0.05
    join_seconds: float = 2.0
    builder: Rebuilder | None = None
    overlay: remote.Overlay | None = None
    revision: int = 0
    probe_seconds: float = 2.0
    connect_media: Callable[[int], Connection] | None = None
    """How a stream reaches the hub's media port. None: this node sends no video or sound."""
    publisher: Callable[..., Publisher] = Publisher
    audio_publisher: Callable[..., Publisher] = AudioPublisher

    _stop: Event = field(default_factory=Event, init=False, repr=False)
    _threads: list[Thread] = field(default_factory=list, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _grant: Grant | None = field(default=None, init=False, repr=False)
    _seen: commands.Seen = field(default_factory=commands.Seen, init=False, repr=False)
    _states: dict[str, SourceState] = field(default_factory=dict, init=False, repr=False)
    _driver_threads: dict[int, Thread] = field(default_factory=dict, init=False, repr=False)
    _swap: Lock = field(default_factory=Lock, init=False, repr=False)
    _configuring: Lock = field(default_factory=Lock, init=False, repr=False)
    _linked_at: float = field(default=0.0, init=False, repr=False)
    _videos: dict[str, Publisher] = field(default_factory=dict, init=False, repr=False)
    """Every stream running, video or sound, by the source it carries."""
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
        self._states = self._fresh_states(self.drivers)

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        log.info("starting as %s", self.identity.summary)
        self.refresh_clock_status()
        self.transport.subscribe(self._topic("commands"), self._on_command)
        self._spawn("network", self._network)
        self._spawn("health", self._health)
        self._start_drivers()

    def stop(self) -> None:
        """Ask everything to stop, and report what did not.

        A driver stuck in a blocking read cannot be interrupted from outside, so its thread
        is a daemon and the process leaves without it. Saying so is the point: silence
        would make an unstoppable sensor look like a clean shutdown.
        """
        self._stop.set()
        for publisher in self._end_videos():
            if not publisher.join(self.join_seconds):
                self.unstopped.append(f"{publisher.kind}:{publisher.stream.source_id}")
        with self._swap:
            running = list(self.drivers)
        self._stop_drivers(running)
        for thread in self._threads:
            thread.join(timeout=self.join_seconds)
            if thread.is_alive():
                self.unstopped.append(thread.name)
                log.warning("%s did not stop", thread.name)
        if self.builder is not None:
            self.builder.release()
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

    def _spawn(self, name: str, target: Callable, *args) -> Thread:
        thread = Thread(target=self._guard, args=(name, target, *args), name=name, daemon=True)
        self._threads.append(thread)
        thread.start()
        return thread

    def _guard(self, name: str, target: Callable, *args) -> None:
        try:
            target(*args)
        except Exception:  # noqa: BLE001 - a thread that dies quietly is a sensor that lies
            log.exception("%s stopped unexpectedly", name)

    def _drive(self, driver: Driver, state: SourceState) -> None:
        try:
            for reading in driver.read():
                if self._stop.is_set() or state.retired:
                    break
                self._record(reading)
                state.readings += 1
                state.last_reading_at = self.clock()
                state.last = reading
        finally:
            state.driver_alive = False

    @staticmethod
    def _fresh_states(drivers: list) -> dict[str, SourceState]:
        return {driver.source_id: SourceState(driver=driver) for driver in drivers}

    def _start_drivers(self) -> None:
        with self._swap:
            running = [(driver, self._states[driver.source_id]) for driver in self.drivers]
        for driver, state in running:
            name = f"driver:{driver.source_id}"
            self._driver_threads[id(driver)] = self._spawn(name, self._drive, driver, state)

    def _stop_drivers(self, drivers: list) -> list[Thread]:
        threads = []
        for driver in drivers:
            state = self._states.get(driver.source_id)
            if state is not None and state.driver is driver:
                state.retired = True
            try:
                driver.stop()
            except Exception:  # noqa: BLE001 - one rude driver must not block the rest
                log.exception("%s raised while stopping", driver.source_id)
            thread = self._driver_threads.pop(id(driver), None)
            if thread is not None:
                threads.append(thread)
        return threads

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
        if not self._linked():
            # Giving up on this attempt without closing it would leave the socket behind
            # and open another one immediately, which is a flood, not a retry.
            log.warning("the hub did not answer the connection in time")
            self.transport.disconnect()
            self._stop.wait(self.reconnect_seconds)
            return
        self._linked_at = self.clock()
        self._publish_state(online=True)
        self._publish_health()

    def _linked(self) -> bool:
        """Wait for the link to come up, because connecting finishes on another thread."""
        deadline = self.clock() + self.link_seconds
        while not self.transport.connected and not self._stop.is_set():
            if self.clock() >= deadline:
                return False
            self._stop.wait(0.05)
        return self.transport.connected

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
        self.spool.put(self._topic("events"), event, size, initial=reading.initial)

    def _send(self, held) -> None:
        queued_ms = self.spool.queued_ms(held)
        delivery = self._delivery(
            queued_ms=queued_ms,
            initial=held.initial,
            # History is what waited for a link, not what took a moment to get through the
            # spool: an event recorded before this link came up is the former.
            replayed=self.clock() - queued_ms / 1000 < self._linked_at,
        )
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
            "config_revision": self.revision,
            "sources": [
                {
                    "source_id": source.id,
                    "kind": source.kind,
                    "enabled": source.enabled,
                    "options": {k: v for k, v in source.options.items() if k != "enabled"},
                }
                for source in self.config.sources
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
            sources=self._source_health(now),
            now=now,
        )
        self._publish_json(self._topic("health"), payload, qos=0, retain=False)

    def _source_health(self, now: float) -> dict:
        with self._lock:
            videos = dict(self._videos)
        sources = {}
        for source_id, state in dict(self._states).items():
            entry = state.as_dict(now)
            publisher = videos.get(source_id)
            if hasattr(state.driver, "argv"):
                entry["video"] = None if publisher is None else publisher.status()
            if hasattr(state.driver, "capture"):
                entry["audio"] = None if publisher is None else publisher.status()
                entry["capture"] = state.driver.capture.status()
                entry["level_dbfs"] = state.driver.level_dbfs
            if hasattr(state.driver, "presence"):
                entry["presence"] = state.driver.presence()
            sources[source_id] = entry
        return sources

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
            self._answer(command, previous, "already handled")
            return
        if command.action == "configure":
            self._begin_configure(command)
            return
        outcome, detail = self._apply(command)
        self._seen.remember(command.command_id, outcome)
        self._answer(command, outcome, detail)

    def _answer(self, command: commands.Command, outcome: commands.Outcome, detail) -> None:
        self._publish_json(
            self._topic("acks"), commands.ack(command, outcome, detail=detail), qos=1, retain=False
        )

    # -- remote configuration ----------------------------------------------------

    def _begin_configure(self, command: commands.Command) -> None:
        """Acknowledge at once and do the work elsewhere.

        Stopping drivers and watching the new ones start takes seconds, and this runs on
        the thread that keeps the link alive. A second request while one is being applied
        is refused without being remembered, so the hub can simply send it again.
        """
        if not self._configuring.acquire(blocking=False):
            self._answer(command, "failed", "another configuration is being applied")
            return
        self._seen.remember(command.command_id, "received")
        self._answer(command, "received", None)
        try:
            self._spawn(f"configure:{command.revision}", self._finish_configure, command)
        except Exception:
            self._configuring.release()
            raise

    def _finish_configure(self, command: commands.Command) -> None:
        try:
            outcome, detail = self._configure(command)
        finally:
            self._configuring.release()
        self._seen.remember(command.command_id, outcome)
        self._answer(command, outcome, detail)

    def _configure(self, command: commands.Command) -> tuple[commands.Outcome, str | None]:
        """Apply a new list of sources, or leave the old one running and say why.

        The request is checked in full, drivers included, before anything stops. Then the
        old drivers stop, the new ones start and are watched for `probe_seconds`; a driver
        that dies or reports an error in that time, or an overlay that cannot be written,
        puts the previous list back.
        """
        if self.builder is None:
            return "failed", "this node does not take its sources from the hub"
        try:
            proposed = remote.check(
                self.config,
                command.revision,
                list(command.sources),
                current=self.revision,
                kinds=self.builder.kinds,
            )
            self.builder.check(proposed)
        except Exception as error:  # noqa: BLE001 - any refusal leaves everything running
            return "failed", str(error)

        previous = self.config
        try:
            self._replace_drivers(proposed)
            problem = self._probe()
        except Exception as error:  # noqa: BLE001 - a driver that cannot start is a refusal
            problem = str(error)
        if problem is None and self.overlay is not None:
            try:
                self.overlay.save(proposed, command.revision)
            except OSError as error:
                problem = f"the configuration could not be kept: {error}"
        if problem is not None:
            log.warning("revision %d not applied: %s", command.revision, problem)
            try:
                self._replace_drivers(previous)
            except Exception as error:  # noqa: BLE001 - say so, rather than die here
                log.exception("the previous sources could not be restored")
                return "failed", f"{problem}; the previous sources did not restart: {error}"
            self._publish_state(online=True)
            return "failed", f"{problem}; revision {self.revision} is still in use"
        self.revision = command.revision
        log.info("sources replaced with revision %d", self.revision)
        self._publish_state(online=True)
        return "applied", f"revision {self.revision}"

    def _replace_drivers(self, config: Config) -> None:
        assert self.builder is not None
        for publisher in self._end_videos():
            publisher.join(self.join_seconds)
        with self._swap:
            old = list(self.drivers)
        for thread in self._stop_drivers(old):
            thread.join(timeout=self.join_seconds)
            if thread.is_alive():
                log.warning("%s did not stop before its replacement started", thread.name)
        built = self.builder(config)
        with self._swap:
            self.config = config
            self.drivers = built
            self._states = self._fresh_states(built)
        self._start_drivers()

    def _probe(self) -> str | None:
        """The first problem a new driver reports while it starts, if any."""
        with self._swap:
            watched = list(self.drivers)
        deadline = time.monotonic() + self.probe_seconds
        while True:
            for driver in watched:
                thread = self._driver_threads.get(id(driver))
                if thread is None or not thread.is_alive():
                    return f"{driver.source_id}: the driver stopped as soon as it started"
                error = getattr(driver, "error", None)
                if error:
                    return f"{driver.source_id}: {error}"
            if time.monotonic() >= deadline:
                return None
            if self._stop.wait(0.05):
                return "the agent is stopping"

    def _apply(self, command: commands.Command) -> tuple[commands.Outcome, str | None]:
        if command.action == "stop":
            self.request_stop()
            return "applied", "stopping"
        if command.action == "revoke":
            with self._lock:
                self._grant = None
            self._end_videos()
            return "applied", None
        if command.action in commands.MEDIA_ACTIONS:
            return self._media(command)
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
                fresh = held is None or held.grant_id != command.grant_id
                self._grant = Grant(
                    grant_id=command.grant_id,
                    capability=command.capability,
                    hub_epoch=command.hub_epoch,
                    expires_at=self.clock() + command.duration_seconds,
                    sequence=command.sequence,
                )
            if fresh:
                self._baselines()
            return "applied", None
        return "failed", "unknown action"

    # -- video and sound ---------------------------------------------------------

    def _media(self, command: commands.Command) -> tuple[commands.Outcome, str | None]:
        """Start, extend or end one camera's or one microphone's stream.

        Streams ride on the events grant: a hub that has not granted this node in its
        current epoch cannot ask for pictures or sound either, and taking the grant away
        ends them.
        """
        kind, _, step = command.action.partition("_")
        now = self.clock()
        with self._lock:
            grant = self._grant
            current = next(
                (
                    p
                    for p in self._videos.values()
                    if p.stream.stream_id == command.stream_id and p.kind == kind
                ),
                None,
            )
        if step == "stop":
            if current is None:
                return "applied", "that stream is not running"
            self._end_video(current)
            return "applied", None
        if grant is None or not grant.valid(now) or grant.capability != GRANT_REQUIRED:
            return "failed", f"{kind} needs a current grant"
        if grant.hub_epoch != command.hub_epoch:
            return "failed", "that command belongs to another run of the hub"
        expires_at = now + command.duration_seconds
        if step == "renew":
            if current is None or not current.alive:
                return "failed", "there is no such stream to renew"
            current.renew(expires_at)
            return "applied", None
        if current is not None and current.alive:
            current.renew(expires_at)
            return "applied", "already streaming"
        if self.connect_media is None:
            return "failed", "this node was started without a way to reach the hub's media port"
        assert command.source_id is not None and command.token is not None
        assert command.stream_id is not None
        with self._swap:
            driver = next((d for d in self.drivers if d.source_id == command.source_id), None)
        stream = Stream(
            stream_id=command.stream_id,
            source_id=command.source_id,
            port=command.port,
            token=command.token,
            hub_epoch=command.hub_epoch,
        )
        if kind == "audio":
            if driver is None or not hasattr(driver, "capture"):
                return "failed", f"{command.source_id} is not a microphone on this node"
            if getattr(driver, "missing", None):
                return "failed", driver.missing
            publisher = self.audio_publisher(
                stream, driver.capture, self.connect_media, expires_at=expires_at, clock=self.clock
            )
        else:
            if driver is None or not hasattr(driver, "argv"):
                return "failed", f"{command.source_id} is not a camera on this node"
            try:
                argv = driver.argv()
            except RuntimeError as error:
                return "failed", str(error)
            publisher = self.publisher(
                stream, argv, self.connect_media, expires_at=expires_at, clock=self.clock
            )
        with self._lock:
            previous = self._videos.get(command.source_id)
            self._videos[command.source_id] = publisher
        if previous is not None:
            previous.stop()
        publisher.start()
        return "applied", None

    def _end_video(self, publisher: Publisher) -> None:
        with self._lock:
            if self._videos.get(publisher.stream.source_id) is publisher:
                del self._videos[publisher.stream.source_id]
        publisher.stop()

    def _end_videos(self) -> list[Publisher]:
        with self._lock:
            publishers = list(self._videos.values())
            self._videos.clear()
        for publisher in publishers:
            publisher.stop()
        return publishers

    def _baselines(self) -> None:
        """Tell a hub that has just granted us where every source stands.

        The hub may have restarted, or may have put a source in doubt over its clock. A
        baseline is what it needs in either case, and it is never taken as news.
        """
        with self._swap:
            running = list(self.drivers)
        for driver in running:
            reading = baseline(driver)
            if reading is not None:
                self._record(reading)

    # -- small things ------------------------------------------------------------

    def _topic(self, channel: str) -> str:
        return protocol.topic(self.identity.node_id, channel)

    def _granted(self) -> bool:
        with self._lock:
            grant = self._grant
        if grant is None or grant.capability != GRANT_REQUIRED:
            return False
        return grant.valid(self.clock())

    def _delivery(
        self, *, queued_ms: int, initial: bool = False, replayed: bool = False
    ) -> protocol.Delivery | None:
        with self._lock:
            grant = self._grant
        if grant is None or not grant.valid(self.clock()):
            return None
        return protocol.Delivery(
            connection_id=self.transport.connection_id,
            hub_epoch=grant.hub_epoch,
            grant_id=grant.grant_id,
            queued_ms=queued_ms,
            replayed=replayed,
            initial_state=initial,
        )

    def refresh_clock_status(self) -> str:
        """Ask the operating system on the health beat, never once per reading.

        Whether this board's clock is trustworthy changes on the scale of minutes, and
        asking costs a subprocess. The hub gets the answer with every heartbeat and makes
        up its own mind regardless.
        """
        self.clock_status = health.clock_status()
        return self.clock_status

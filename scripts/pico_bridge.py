#!/usr/bin/env python3
"""Carry a wired Pico's messages to the broker, and the hub's commands back.

A Pico or Pico 2 without a W has no radio, so it cannot be a satellite on its own. What it
can do is speak down the USB cable that powers it, to a machine that already is one. This
script is that machine's half: it opens the port, frames and unframes what crosses it with
`sentry_mode.satellites.link`, and publishes what the board says on the board's own topics
with the board's own certificate.

    scripts/pico_bridge.py --config .local/pico-bridge.json
    scripts/pico_bridge.py --config .local/pico-bridge.json --console pico-cablato

Three rules decide what this program is, and they are the reason it is this small.

**It is not a second rule engine.** It does not read an event, decide anything about it, or
change it. A frame's payload is published exactly as the board wrote it; a command is
handed over exactly as the hub sent it. The hub's rules run on the hub.

**A node is trusted because somebody plugged it in and wrote it down here.** The USB serial
number is not an identity — it is a string a device chooses for itself — so the identity
comes from this file's mapping of port to node, and from nothing else. A board whose
messages claim another node's name has those messages refused and counted, not published:
otherwise anything plugged into this machine could speak as any satellite.

**Unplugged is offline.** The bridge connects to the broker with the node's goodbye as its
will, so the hub hears about a bridge that was killed as quickly as about one that said so.
When the cable goes, the goodbye is published straight away, and the hub's own machinery
does the rest: the session ends and the node's sources go to unknown, because nothing is
reporting them any more.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import select
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sentry_mode.satellites import link  # noqa: E402  (after the path is set)

log = logging.getLogger("pico-bridge")

TIME_EVERY = 60.0
"""How often the board is told what time it is. It has no clock of its own across a power
cut and no network to ask, so this is the only thing that makes its readings stamped.

Zero means never, which `--time-every 0` asks for: a board that has been told the time once
and then hears nothing more is how the three-hour rule — the one that stops a node calling
what it stamps `synced` when its time source has gone — is watched happening, and there is
no other way to take a time source away from a board that has no radio."""

CONNECT_EVERY = 5.0
"""How often to try the broker again. A broker that is not there is not a reason to stop
reading the cable: the board goes on queueing, and this end goes on asking."""

HELLO_AFTER = 5.0
"""How long to wait for a board to answer a hello before saying it again. A board that was
already running when the cable was plugged in has nothing to announce until it is asked."""

QUIET_FOR = 45.0
"""How long a board may say nothing before it is asked again who it is. A board reports how
it is every fifteen seconds, so this is a board that reset without the port going with it,
or a hello that was lost. Either way what it needs is to be asked again."""


class Misconfigured(SystemExit):
    """The file describing which board is which is not usable."""


@dataclass(frozen=True)
class Broker:
    host: str
    port: int = 8883
    ca: str = ""
    prefix: str = "sentry/v1"
    keepalive: int = 30


@dataclass(frozen=True)
class Wired:
    """One board, the port it is plugged into and the identity that port carries."""

    node_id: str
    port: str
    certificate: str
    key: str


def read_configuration(where: Path) -> tuple[Broker, list[Wired]]:
    try:
        document = json.loads(where.read_text())
    except (OSError, ValueError) as problem:
        raise Misconfigured(f"{where}: {problem}") from None
    said = document.get("broker") or {}
    if not said.get("host"):
        raise Misconfigured(f"{where}: no broker host")
    broker = Broker(
        host=str(said["host"]),
        port=int(said.get("port", 8883)),
        ca=str(said.get("ca", "")),
        prefix=str(said.get("prefix", "sentry/v1")),
        keepalive=int(said.get("keepalive", 30)),
    )
    boards: list[Wired] = []
    for entry in document.get("nodes") or []:
        for needed in ("node_id", "port", "certificate", "key"):
            if not entry.get(needed):
                raise Misconfigured(f"{where}: a node with no {needed}")
        boards.append(
            Wired(
                node_id=str(entry["node_id"]),
                port=str(entry["port"]),
                certificate=str(entry["certificate"]),
                key=str(entry["key"]),
            )
        )
    if not boards:
        raise Misconfigured(f"{where}: no boards to bridge")
    names = [one.node_id for one in boards]
    if len(set(names)) != len(names):
        raise Misconfigured(f"{where}: two ports claiming the same node")
    return broker, boards


def whose(payload: bytes) -> str | None:
    """The node a message is about, as the message itself says.

    Read only to be checked against the mapping, never to decide where something goes: the
    topic is built from the configured name, so a board that named itself something else
    has its message refused rather than published somewhere it does not belong.
    """
    try:
        document = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    said = document.get("node_id")
    if isinstance(said, str):
        return said
    inner = document.get("event")
    if isinstance(inner, dict) and isinstance(inner.get("node_id"), str):
        return inner["node_id"]
    return None


def goodbye_for(state: dict) -> bytes:
    """The will: who is leaving, which boot and which connection, and that it is gone."""
    return json.dumps(
        {
            "schema_version": 1,
            "node_id": state["node_id"],
            "boot_id": state["boot_id"],
            "connection_id": state["connection_id"],
            "online": False,
        },
        separators=(",", ":"),
    ).encode()


@dataclass
class Counts:
    frames: int = 0
    published: int = 0
    commands: int = 0
    refused: int = 0
    discarded: int = 0
    missed: int = 0

    def as_line(self) -> str:
        return (
            f"frames={self.frames} published={self.published} commands={self.commands} "
            f"refused={self.refused} discarded={self.discarded} missed={self.missed}"
        )


@dataclass
class Carried:
    """One bridged board: its port, its broker connection, and what has crossed between."""

    who: Wired
    broker: Broker
    port: object | None = None
    client: object | None = None
    reader: link.Reader = field(default_factory=lambda: link.Reader(expect_from_the_node=True))
    counter: int = 0
    commands: queue.Queue = field(default_factory=queue.Queue)
    state: dict | None = None  # the last announcement, which is what the will is made of
    state_payload: bytes | None = None  # and the bytes of it, for saying it again
    waiting_to_connect: dict | None = None
    tried_to_connect_at: float = 0.0
    said_hello_at: float = 0.0
    told_the_time_at: float = 0.0
    heard_at: float = 0.0
    counts: Counts = field(default_factory=Counts)

    # -- the cable ---------------------------------------------------------------------

    def open_the_port(self) -> bool:
        # A bridge runs where the board is. The hub itself needs neither of these, which is
        # why they are an extra rather than a dependency.
        try:
            import serial
        except ImportError:
            raise Misconfigured(
                "a bridge needs pyserial: install sentry-mode with the 'bridge' extra"
            ) from None

        try:
            self.port = serial.Serial(self.who.port, 115200, timeout=0, exclusive=True)
        except (OSError, serial.SerialException) as problem:
            log.debug("%s: %s", self.who.node_id, problem)
            self.port = None
            return False
        self.reader = link.Reader(expect_from_the_node=True)
        self.counter = 0
        self.said_hello_at = 0.0
        self.told_the_time_at = 0.0
        self.heard_at = time.monotonic()
        log.info("%s: %s is open", self.who.node_id, self.who.port)
        return True

    def fileno(self) -> int:
        return self.port.fileno() if self.port is not None else -1

    def write(self, carries: link.Carries, payload: bytes) -> bool:
        if self.port is None:
            return False
        try:
            self.port.write(link.pack(link.Frame(carries, self.counter, payload)))
        except (OSError, link.LinkError) as problem:
            log.warning("%s: %s", self.who.node_id, problem)
            self.let_go("the port would not take a frame")
            return False
        self.counter = (self.counter + 1) % 0x100000000
        return True

    def let_go(self, why: str) -> None:
        """The cable is gone. Say so on the broker, then close everything."""
        if self.port is not None:
            log.info("%s: %s", self.who.node_id, why)
            try:
                self.port.close()
            except OSError:
                pass
            self.port = None
        if self.client is not None and self.state is not None:
            # Straight away, rather than leaving it to the will: the will is what covers
            # this bridge being killed, and a cable pulled out is not that.
            self.publish("state", goodbye_for(self.state), retain=True)
        self.state = None
        self.state_payload = None
        self.waiting_to_connect = None
        self.disconnect()

    # -- the broker --------------------------------------------------------------------

    def connect(self, state: dict) -> None:
        """Open this node's own connection, with this node's own goodbye as its will.

        Not before the board has announced itself: a will has to name the connection it is
        about, and until the board has said which one that is there is no will to set.
        """
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise Misconfigured(
                "a bridge needs paho-mqtt: install sentry-mode with the 'bridge' extra"
            ) from None

        self.disconnect()
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bridge-{self.who.node_id}",
            protocol=mqtt.MQTTv5,
            clean_session=None,
        )
        client.tls_set(
            ca_certs=self.broker.ca or None,
            certfile=self.who.certificate,
            keyfile=self.who.key,
        )
        client.will_set(self.topic("state"), goodbye_for(state), qos=1, retain=True)
        client.on_message = self._a_command_arrived
        client.on_connect = self._connected
        client.connect(self.broker.host, self.broker.port, keepalive=self.broker.keepalive)
        client.loop_start()
        self.client = client
        self.state = state

    def disconnect(self) -> None:
        if self.client is None:
            return
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except OSError:
            pass
        self.client = None

    def topic(self, channel: str) -> str:
        return f"{self.broker.prefix}/nodes/{self.who.node_id}/{channel}"

    def _connected(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("%s: the broker refused this node: %s", self.who.node_id, reason_code)
            return
        client.subscribe(self.topic("commands"), qos=1)
        # And say again who is here. A reconnection is not always to a broker that remembers
        # anything: one that restarted lost every retained message with it, and this node's
        # presence is one of them. The board has nothing new to say — the cable never moved,
        # so it never noticed — and a node nobody announced is a node the hub calls offline
        # while it goes on publishing into it.
        if self.state_payload is not None:
            client.publish(self.topic("state"), self.state_payload, qos=1, retain=True)
        log.info("%s: connected to %s", self.who.node_id, self.broker.host)

    def _a_command_arrived(self, client, userdata, message) -> None:
        # On paho's thread. Nothing is decided here: it goes in a queue and down the cable
        # on the next turn of the loop, exactly as it arrived.
        self.commands.put(bytes(message.payload))

    def publish(self, channel: str, payload: bytes, retain: bool = False) -> bool:
        if self.client is None:
            return False
        try:
            self.client.publish(self.topic(channel), payload, qos=1, retain=retain)
        except (OSError, ValueError) as problem:
            log.warning("%s: %s", self.who.node_id, problem)
            return False
        self.counts.published += 1
        return True

    # -- what crosses -------------------------------------------------------------------

    def read_what_arrived(self) -> None:
        if self.port is None:
            return
        try:
            waiting = self.port.read(4096)
        except OSError as problem:
            self.let_go(f"the port stopped answering: {problem}")
            return
        if not waiting:
            return
        try:
            frames = self.reader.feed(waiting)
        except link.LinkError as problem:
            # A frame from the side that never sends it: a board sending commands, or
            # something on this port that is not this firmware at all.
            log.warning("%s: %s", self.who.node_id, problem)
            self.counts.refused += 1
            self.reader = link.Reader(expect_from_the_node=True)
            return
        for frame in frames:
            self.heard_at = time.monotonic()
            self.take(frame)
        self.counts.frames = self.reader.frames
        self.counts.discarded = self.reader.discarded
        self.counts.missed = self.reader.missed

    def take(self, frame: link.Frame) -> None:
        if frame.carries == link.Carries.SAID:
            # The board's console. It goes where a person can read it and nowhere else:
            # nothing here publishes a line of diagnostics as though it were a message.
            for line in frame.payload.decode("utf-8", "replace").splitlines():
                log.info("%s | %s", self.who.node_id, line)
            return
        channel = frame.channel
        if channel is None:
            return
        claimed = whose(frame.payload)
        if claimed is not None and claimed != self.who.node_id:
            # The mapping says which node this port is. A board that named itself something
            # else is either misconfigured or is not the board that was plugged in here.
            log.error(
                "%s: a message claiming to be %s was not published", self.who.node_id, claimed
            )
            self.counts.refused += 1
            return
        log.debug("%s %s %s", self.who.node_id, channel, frame.payload.decode("utf-8", "replace"))
        if channel == "state":
            self.a_state_arrived(frame.payload)
            return
        self.publish(channel, frame.payload)

    def a_state_arrived(self, payload: bytes) -> None:
        try:
            state = json.loads(payload)
        except ValueError:
            self.counts.refused += 1
            return
        if not all(isinstance(state.get(one), str) for one in ("node_id", "boot_id")):
            self.counts.refused += 1
            return
        if not isinstance(state.get("connection_id"), str):
            self.counts.refused += 1
            return
        new = self.state is None or state["connection_id"] != self.state["connection_id"]
        self.state = state
        # Kept as it arrived, byte for byte, because it may have to be said again: see
        # `_connected`. What the board wrote is what the hub reads.
        self.state_payload = payload
        if new and state.get("online"):
            # A connection this bridge has no will for. It opens one that has it, so that
            # this process being killed reads on the hub as this node going away.
            self.waiting_to_connect = state
            self.tried_to_connect_at = 0.0
            self.reach_the_broker(time.monotonic())
        self.publish("state", payload, retain=True)

    def reach_the_broker(self, now: float) -> None:
        """Open this node's connection, and keep trying until it is open.

        A broker that is not there, or a certificate it will not take, is not a reason to
        stop reading the cable: the board goes on taking readings and queueing them, and
        this end goes on asking. What it must not do is fall over and leave a board
        talking to nobody.
        """
        if self.waiting_to_connect is None or self.client is not None:
            return
        if now - self.tried_to_connect_at < CONNECT_EVERY:
            return
        self.tried_to_connect_at = now
        try:
            self.connect(self.waiting_to_connect)
        except Exception as problem:  # paho raises OSError, ssl.SSLError and its own
            log.error("%s: %s", self.who.node_id, problem)
            self.disconnect()
            return
        self.waiting_to_connect = None

    def hand_over_commands(self) -> None:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            if len(command) > link.MAX_PAYLOAD:
                log.error("%s: a command too long for the cable was dropped", self.who.node_id)
                self.counts.refused += 1
                continue
            if self.write(link.Carries.COMMANDS, command):
                self.counts.commands += 1

    def keep_it_going(self, now: float) -> None:
        self.reach_the_broker(now)
        if self.port is None:
            return
        if now - self.heard_at > QUIET_FOR:
            # A board that reset without taking the port with it, or a hello that was lost.
            # It is asked again rather than waited on: a board waiting for a hello and a
            # bridge waiting for an announcement would wait for each other for ever.
            log.warning("%s: nothing for %.0fs; asking again", self.who.node_id, QUIET_FOR)
            self.heard_at = now
            self.state = None
            self.state_payload = None
        if self.state is None and now - self.said_hello_at > HELLO_AFTER:
            # A board already running when the cable was plugged in has nothing to announce
            # until it is asked, so it is asked until it answers.
            self.said_hello_at = now
            self.write(link.Carries.HELLO, b"")
        if TIME_EVERY > 0 and now - self.told_the_time_at > TIME_EVERY:
            self.told_the_time_at = now
            unix_ms = int(time.time() * 1000)
            self.write(link.Carries.TIME, json.dumps({"unix_ms": unix_ms}).encode())


def run(broker: Broker, boards: list[Wired], console: str | None) -> int:
    carried = [Carried(who=one, broker=broker) for one in boards]
    if console is not None and console not in {one.node_id for one in boards}:
        raise Misconfigured(f"there is no board called {console} to type at")
    log.info("bridging %s", ", ".join(f"{one.node_id} on {one.port}" for one in boards))
    try:
        while True:
            now = time.monotonic()
            for one in carried:
                if one.port is None:
                    one.open_the_port()
                one.keep_it_going(now)
                one.hand_over_commands()
            watching = [one.fileno() for one in carried if one.port is not None]
            if console is not None:
                watching.append(sys.stdin.fileno())
            ready, _, _ = select.select(watching, [], [], 0.25) if watching else ([], [], [])
            for one in carried:
                if one.port is not None and one.fileno() in ready:
                    one.read_what_arrived()
            if console is not None and sys.stdin.fileno() in ready:
                typed = sys.stdin.readline()
                if typed:
                    at = next(one for one in carried if one.who.node_id == console)
                    at.write(link.Carries.TYPED, typed.encode())
    except KeyboardInterrupt:
        pass
    finally:
        for one in carried:
            log.info("%s: %s", one.who.node_id, one.counts.as_line())
            one.let_go("the bridge is stopping")
    return 0


def main() -> int:
    global TIME_EVERY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(".local/pico-bridge.json"))
    parser.add_argument(
        "--console",
        metavar="NODE",
        help="send what is typed here to that board's console, for provisioning it",
    )
    parser.add_argument(
        "--time-every",
        type=float,
        default=TIME_EVERY,
        metavar="SECONDS",
        help="how often to tell the boards what time it is; 0 never tells them again",
    )
    parser.add_argument("--verbose", action="store_true")
    arguments = parser.parse_args()
    if arguments.time_every < 0:
        raise SystemExit("--time-every takes seconds, or 0 for never")
    TIME_EVERY = arguments.time_every
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if TIME_EVERY == 0:
        # Said out loud, because a board whose stamps stop being called synced three hours
        # from now is a thing somebody should be able to find the reason for in this log.
        log.warning("nothing here will tell the boards what time it is")
    broker, boards = read_configuration(arguments.config)
    return run(broker, boards, arguments.console)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Decode the firmware's MQTT packets with an implementation it has never seen.

The C++ codec was written from the MQTT 3.1.1 specification; so was the decoder below, and
neither has read the other. What comes out is then handed to the hub's own models: the will
must be the goodbye the hub would accept, the topics must be topics the hub's parser reads
back as this node's, and the payloads must be the messages the contract describes.

    firmware/pico/tools/mqtt_check.py --build-dir build/pico-host

A codec that agrees with the tests written beside it agrees with itself. This is the check
that it agrees with the broker and the hub.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTRACTS = REPO / "contracts" / "satellite" / "v1"

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "satellite" / "tests"))
sys.path.insert(0, str(REPO / "satellite" / "src"))

import schema as subset  # noqa: E402  (after the path is set)
from sentry_satellite.protocol import topic as agent_topic  # noqa: E402

from sentry_mode.satellites.control import MESSAGES  # noqa: E402
from sentry_mode.satellites.mqtt import parse_topic  # noqa: E402
from sentry_mode.satellites.protocol import EventEnvelope  # noqa: E402

NODE = "pico-ingresso"


class NotAPacket(Exception):
    """The bytes are not a packet this decoder can read, which is the finding."""


def take_length(data: bytes, at: int) -> tuple[int, int]:
    """The remaining length: seven bits a byte, at most four bytes."""
    value = 0
    multiplier = 1
    for index in range(4):
        if at + index >= len(data):
            raise NotAPacket("the length ran off the end of the packet")
        byte = data[at + index]
        value += (byte & 0x7F) * multiplier
        multiplier *= 128
        if not byte & 0x80:
            return value, index + 1
    raise NotAPacket("a remaining length of more than four bytes")


def take_text(data: bytes, at: int) -> tuple[bytes, int]:
    if at + 2 > len(data):
        raise NotAPacket("a string with no length")
    length = (data[at] << 8) | data[at + 1]
    end = at + 2 + length
    if end > len(data):
        raise NotAPacket(f"a string of {length} bytes with {len(data) - at - 2} left")
    return data[at + 2 : end], end


def decode(data: bytes) -> dict:
    if len(data) < 2:
        raise NotAPacket("shorter than a header")
    kind = data[0] >> 4
    flags = data[0] & 0x0F
    body, length_bytes = take_length(data, 1)
    whole = 1 + length_bytes + body
    if whole != len(data):
        raise NotAPacket(f"says {body} bytes of body and {len(data) - 1 - length_bytes} arrived")
    at = 1 + length_bytes

    if kind == 1:
        name, at = take_text(data, at)
        if name != b"MQTT":
            raise NotAPacket(f"the protocol is called {name!r}")
        level, connect_flags = data[at], data[at + 1]
        keepalive = (data[at + 2] << 8) | data[at + 3]
        at += 4
        client_id, at = take_text(data, at)
        packet = {
            "type": "connect",
            "level": level,
            "clean_session": bool(connect_flags & 0x02),
            "keepalive": keepalive,
            "client_id": client_id.decode(),
            "will": None,
        }
        if connect_flags & 0x04:
            topic, at = take_text(data, at)
            payload, at = take_text(data, at)
            packet["will"] = {
                "topic": topic.decode(),
                "payload": payload,
                "qos": (connect_flags >> 3) & 0x03,
                "retain": bool(connect_flags & 0x20),
            }
        if connect_flags & 0x80:
            username, at = take_text(data, at)
            packet["username"] = username.decode()
        if connect_flags & 0x40:
            raise NotAPacket("a password, where the certificate is the identity")
        if at != len(data):
            raise NotAPacket(f"{len(data) - at} bytes nobody accounted for")
        return packet

    if kind == 3:
        qos = (flags >> 1) & 0x03
        topic, at = take_text(data, at)
        packet_id = None
        if qos:
            packet_id = (data[at] << 8) | data[at + 1]
            at += 2
        return {
            "type": "publish",
            "topic": topic.decode(),
            "payload": data[at:],
            "qos": qos,
            "retain": bool(flags & 0x01),
            "duplicate": bool(flags & 0x08),
            "packet_id": packet_id,
        }

    if kind == 8:
        if flags != 0x02:
            raise NotAPacket(f"SUBSCRIBE with reserved bits {flags:#x}; brokers refuse that")
        packet_id = (data[at] << 8) | data[at + 1]
        at += 2
        topic, at = take_text(data, at)
        qos = data[at]
        return {"type": "subscribe", "packet_id": packet_id, "topic": topic.decode(), "qos": qos}

    if kind == 4:
        return {"type": "puback", "packet_id": (data[at] << 8) | data[at + 1]}
    if kind == 12:
        return {"type": "pingreq"}
    if kind == 14:
        return {"type": "disconnect"}
    raise NotAPacket(f"packet type {kind}")


def emitted(binary: Path) -> dict[str, bytes]:
    finished = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
    if finished.returncode != 0:
        raise SystemExit(f"{binary.name} refused to write a packet:\n{finished.stderr}")
    packets: dict[str, bytes] = {}
    for line in finished.stdout.splitlines():
        if not line.strip():
            continue
        name, _, body = line.partition("\t")
        packets[name] = bytes.fromhex(body)
    return packets


def judged(document: dict, model, schema: dict, what: str) -> None:
    try:
        model.model_validate(document)
    except Exception as problem:  # noqa: BLE001 — the message is the whole point
        raise SystemExit(f"{what} is not something the hub would take:\n{problem}") from problem
    try:
        subset.validate(document, schema)
    except subset.Invalid as problem:
        raise SystemExit(f"{what} does not match the published schema: {problem}") from problem


def channel_of(topic: str, what: str) -> str:
    """The hub's own parser, on the topic the firmware built.

    The hub parses only what a node publishes, so `commands` — the one topic a node reads
    rather than writes — is compared with what the Linux agent would have subscribed to.
    """
    if topic == agent_topic(NODE, "commands"):
        return "commands"
    read = parse_topic(topic, "sentry/v1")
    if read is None:
        raise SystemExit(f"the hub's parser does not recognise {topic!r} ({what})")
    node_id, channel = read
    if node_id != NODE:
        raise SystemExit(f"{topic!r} belongs to {node_id}, not {NODE}")
    return channel


def conversation(binary: Path) -> int:
    """One whole connection, in the order a broker would have seen it.

    Each packet on its own being well formed is not the same as the node behaving. What is
    checked here is the order and the memory: nothing is announced before the subscription
    is confirmed, an unacknowledged event comes back as the same event rather than a second
    one, and a command is answered.
    """
    if not binary.exists():
        raise SystemExit(f"{binary} is not built; cmake --build {binary.parent}")
    try:
        packets = {name: decode(body) for name, body in emitted(binary).items()}
    except NotAPacket as problem:
        raise SystemExit(f"the client wrote something that is not a packet: {problem}") from None

    order = list(packets)
    expected = [
        "1.connect",
        "2.subscribe",
        "3.publish.state",
        "4.publish.event",
        "5.republish.event",
        "6.puback",
        "7.ping",
    ]
    if order != expected:
        raise SystemExit(f"the conversation went {order}, not {expected}")

    if packets["1.connect"]["client_id"] != NODE or not packets["1.connect"]["clean_session"]:
        raise SystemExit("the connection is not a clean one made by this node")
    if channel_of(packets["2.subscribe"]["topic"], "the subscription") != "commands":
        raise SystemExit("the node subscribed to something other than its commands")

    hello = packets["3.publish.state"]
    if not hello["retain"] or hello["qos"] != 1:
        raise SystemExit("the announcement is not a retained, acknowledged state")
    document = json.loads(hello["payload"])
    judged(
        document,
        MESSAGES["state"],
        json.loads((CONTRACTS / "control/state.schema.json").read_text()),
        "the announcement",
    )
    if document.get("online") is not True:
        raise SystemExit("the node announced itself without saying it was here")

    first = packets["4.publish.event"]
    again = packets["5.republish.event"]
    if first["duplicate"]:
        raise SystemExit("the first attempt at an event claims to be a repeat of one")
    if not again["duplicate"]:
        raise SystemExit("an event sent twice without DUP is two events to a broker")
    if again["packet_id"] != first["packet_id"] or again["payload"] != first["payload"]:
        raise SystemExit("the repeat is a different event, not the same one again")
    judged(
        json.loads(first["payload"]),
        EventEnvelope,
        json.loads((CONTRACTS / "event.schema.json").read_text()),
        "the event",
    )
    if first["packet_id"] == hello["packet_id"]:
        raise SystemExit("two messages in flight under one packet id")

    if packets["6.puback"]["packet_id"] != 42:
        raise SystemExit("the command was acknowledged under somebody else's packet id")
    if packets["7.ping"]["type"] != "pingreq":
        raise SystemExit("a quiet link was not pinged")
    return len(packets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build/pico-host"))
    arguments = parser.parse_args(argv)

    binary = arguments.build_dir / "sentry_mqtt_emit"
    if not binary.exists():
        raise SystemExit(f"{binary} is not built; cmake --build {arguments.build_dir}")

    try:
        packets = {name: decode(body) for name, body in emitted(binary).items()}
    except NotAPacket as problem:
        raise SystemExit(f"the firmware wrote something that is not a packet: {problem}") from None

    connect = packets["connect"]
    if connect["level"] != 4:
        raise SystemExit(f"the CONNECT says protocol level {connect['level']}, not 3.1.1")
    if not connect["clean_session"]:
        raise SystemExit("a satellite that kept a session could skip confirming its subscription")
    if connect["client_id"] != NODE:
        raise SystemExit(f"the client id is {connect['client_id']!r}")
    will = connect["will"]
    if will is None:
        raise SystemExit("the CONNECT registers no will; nothing would say this node had gone")
    if will["qos"] != 1 or not will["retain"]:
        raise SystemExit(f"the will is qos {will['qos']}, retain {will['retain']}")
    if channel_of(will["topic"], "the will") != "state":
        raise SystemExit(f"the will goes to {will['topic']}, which is not the state topic")
    if len(will["topic"]) + len(will["payload"]) > 255:
        raise SystemExit("the will is larger than lwIP will carry in a CONNECT")
    goodbye = json.loads(will["payload"])
    judged(
        goodbye,
        MESSAGES["state"],
        json.loads((CONTRACTS / "control/state.schema.json").read_text()),
        "the will",
    )
    if goodbye.get("online") is not False:
        raise SystemExit("the will does not say this node is gone")

    subscribe = packets["subscribe"]
    if channel_of(subscribe["topic"], "the subscription") != "commands":
        raise SystemExit(f"the node subscribes to {subscribe['topic']}")
    if subscribe["qos"] != 1 or subscribe["packet_id"] == 0:
        raise SystemExit("a subscription that cannot be confirmed is not one")

    event_schema = json.loads((CONTRACTS / "event.schema.json").read_text())
    control = {
        name: json.loads((CONTRACTS / "control" / f"{name}.schema.json").read_text())
        for name in MESSAGES
    }
    published = 0
    for name, packet in packets.items():
        if packet["type"] != "publish":
            continue
        published += 1
        channel = channel_of(packet["topic"], name)
        document = json.loads(packet["payload"])
        if channel == "events":
            if packet["qos"] != 1:
                raise SystemExit("an event published at most once is an event that can be lost")
            judged(document, EventEnvelope, event_schema, name)
        elif channel == "state":
            if not packet["retain"]:
                raise SystemExit("a state nobody retains is a node that vanishes on reconnect")
            judged(document, MESSAGES["state"], control["state"], name)
        elif channel in ("health", "acks"):
            judged(
                document,
                MESSAGES[channel[:-1] if channel == "acks" else channel],
                control[channel[:-1] if channel == "acks" else channel],
                name,
            )
        else:
            raise SystemExit(f"{name} goes to {channel}, which a node does not publish to")

    if packets["puback"]["packet_id"] == 0:
        raise SystemExit("a PUBACK that names nothing acknowledges nothing")
    for name in ("pingreq", "disconnect"):
        if packets[name]["type"] != name:
            raise SystemExit(f"{name} decoded as {packets[name]['type']}")

    print(f"{len(packets)} packets decoded from the specification, {published} of them published")
    print("the will, the subscription and every payload are what the hub would accept")

    spoken = conversation(arguments.build_dir / "sentry_client_run")
    print(f"and {spoken} more in one connection, in the order the client wrote them:")
    print("nothing announced before the subscription, and an event resent as the same event")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

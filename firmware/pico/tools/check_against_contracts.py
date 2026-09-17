#!/usr/bin/env python3
"""Hold the firmware's own serializer to the contract, judged by the other two sides.

The C++ under src/protocol was written from the same schema as everything else, and a
serializer checked against the schema it was written from proves very little. So this runs
the firmware's writers for real and hands every line they produce to the hub's Pydantic
models and to the satellite's schema checker — two implementations that have never seen
the C++ and do not agree with it by construction.

Three things are checked: the events, the three control messages this node writes, and the
topics it writes them to, which are compared against the agent's own topic function and
the hub's own topic parser.

    firmware/pico/tools/check_against_contracts.py --build-dir build/pico-host

The build directory is whatever `cmake -S firmware/pico -B <dir>` made; the script only
needs the `sentry_emit` binary inside it.
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
from sentry_mode.satellites.mqtt import CHANNELS, parse_topic  # noqa: E402
from sentry_mode.satellites.protocol import EventEnvelope  # noqa: E402
from sentry_mode.sources.models import ClockStatus, Quality  # noqa: E402


def emitted(binary: Path, *arguments: str) -> list[str]:
    finished = subprocess.run(
        [str(binary), *arguments], capture_output=True, text=True, check=False
    )
    if finished.returncode != 0:
        raise SystemExit(f"{binary.name} refused to write what it was asked:\n{finished.stderr}")
    return [line for line in finished.stdout.splitlines() if line.strip()]


def judge(document: dict, model: type, schema: dict, what: str) -> None:
    """The hub's models and the agent's schema checker, on the same document."""
    try:
        model.model_validate(document)
    except Exception as problem:  # noqa: BLE001 — the message is the whole point
        raise SystemExit(f"{what} is not something the hub would take:\n{problem}") from problem
    try:
        subset.validate(document, schema)
    except subset.Invalid as problem:
        raise SystemExit(f"{what} does not match the published schema: {problem}") from problem


def check_control(binary: Path) -> int:
    """The state, the health and the answers this node writes."""
    schemas = {
        name: json.loads((CONTRACTS / "control" / f"{name}.schema.json").read_text())
        for name in MESSAGES
    }
    lines = emitted(binary, "control")
    if not lines:
        raise SystemExit("the firmware wrote no control messages")
    said = set()
    bridged = False
    for number, line in enumerate(lines, start=1):
        name, _, payload = line.partition("\t")
        if name not in MESSAGES:
            raise SystemExit(f"line {number} is a {name}, which is not a control message")
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as problem:
            raise SystemExit(f"the {name} on line {number} is not JSON: {problem}") from problem
        judge(document, MESSAGES[name], schemas[name], f"the {name} on line {number}")
        said.add(name)
        bridged = bridged or document.get("reached_by") == "bridge"

        # A goodbye is the will the broker holds, and lwIP writes a will with 8-bit
        # lengths: topic and payload together have to fit in 255 bytes.
        if name == "state" and document["online"] is False:
            # Measured for the longest name the contract allows, not for the one that
            # happens to be in the fixture: that is the case with one byte to spare.
            longest = 40 - len(document["node_id"])
            topic = f"sentry/v1/nodes/{'a' * 40}/state"
            carried = len(payload) + longest + len(topic)
            if carried > 255:
                raise SystemExit(
                    f"a goodbye from a node with the longest allowed name would be "
                    f"{carried} bytes with its topic, which is more than a will can carry"
                )

    written = {name for name in MESSAGES if name != "command"}
    if said != written:
        raise SystemExit(f"the firmware never wrote a {', '.join(sorted(written - said))}")
    # A board with no radio says that something else is carrying what it says, and the
    # hub's model has one word for it. A firmware that stopped saying it, or started
    # spelling it differently, would leave the hub unable to tell the two kinds apart.
    if not bridged:
        raise SystemExit("the firmware never said how a bridged node is reached")
    print(f"{len(lines)} control messages written by the firmware, taken by the hub too")
    return len(lines)


def check_topics(binary: Path) -> int:
    """Where it writes them, judged by the two implementations that already talk."""
    lines = emitted(binary, "topics")
    seen = {}
    for line in lines:
        channel, _, topic = line.partition("\t")
        seen[channel] = topic
        if channel == "commands":
            continue  # the hub sends these; it does not parse them from a node
        found = parse_topic(topic, "sentry/v1")
        if found is None:
            raise SystemExit(f"the hub does not recognise {topic}")
        node_id, parsed = found
        if parsed != channel:
            raise SystemExit(f"{topic} reads as a {parsed} to the hub, not a {channel}")
        if agent_topic(node_id, channel) != topic:
            raise SystemExit(f"the agent would have written {agent_topic(node_id, channel)}")
    missing = set(CHANNELS) - set(seen)
    if missing:
        raise SystemExit(f"the firmware has no topic for {', '.join(sorted(missing))}")
    print(f"{len(seen)} topics, read back by the hub's parser and matching the agent's")
    return len(seen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=REPO / "build" / "pico-host",
        help="the cmake build directory holding sentry_emit",
    )
    arguments = parser.parse_args()

    binary = arguments.build_dir / "sentry_emit"
    if not binary.exists():
        raise SystemExit(
            f"{binary} is not there. Build it first:\n"
            f"  cmake -S firmware/pico -B {arguments.build_dir} -G Ninja\n"
            f"  cmake --build {arguments.build_dir}"
        )

    published = json.loads((CONTRACTS / "event.schema.json").read_text())
    lines = emitted(binary)
    if not lines:
        raise SystemExit("the firmware wrote nothing, which is not a passing check")

    for number, line in enumerate(lines, start=1):
        try:
            document = json.loads(line)
        except json.JSONDecodeError as problem:
            raise SystemExit(f"line {number} is not JSON at all: {problem}") from problem
        try:
            envelope = EventEnvelope.model_validate(document)
        except Exception as problem:  # noqa: BLE001 — the message is the whole point
            raise SystemExit(
                f"line {number} is not an event the hub would take:\n{problem}"
            ) from problem
        try:
            subset.validate(document, published)
        except subset.Invalid as problem:
            raise SystemExit(
                f"line {number} does not match the published schema: {problem}"
            ) from problem
        # The firmware writes the value itself, and a reading that arrives as a different
        # number than it left is the failure this whole exercise exists to catch.
        if envelope.event.value != document["event"]["value"]:
            raise SystemExit(f"line {number} changed value on the way through the hub's models")

    # Every word the firmware can put in these two fields is exercised by the emitter, so
    # a vocabulary that drifted from the hub's is a failure here rather than on the wire.
    for field, vocabulary in (("quality", Quality), ("clock_status", ClockStatus)):
        said = {json.loads(line)["event"][field] for line in lines}
        missing = {member.value for member in vocabulary} - said
        if missing:
            raise SystemExit(
                f"the firmware never wrote {field} "
                + ", ".join(sorted(missing))
                + ", so nothing here checked those words"
            )

    print(f"{len(lines)} events written by the firmware, taken by the hub and by the schema")
    check_control(binary)
    check_topics(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

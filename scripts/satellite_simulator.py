#!/usr/bin/env python3
"""Produce the traffic a satellite would send, including the traffic that goes wrong.

The hub has to survive a node whose clock is hours out, a node that reconnects and floods,
a node that repeats itself and a node that has simply stopped noticing anything. None of
those are easy to arrange on a real board on demand, so they are written out here as
scenarios and replayed byte for byte.

    python scripts/satellite_simulator.py --scenario backlog
    python scripts/satellite_simulator.py --list
    python scripts/satellite_simulator.py --scenario all --out .local/satellite-traffic.jsonl

The output is one JSON message per line, exactly as it would arrive on the events topic.
A simulated event carries a reading and nothing else: no shell command, no rule, no path.
"""

import argparse
import json
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "satellite" / "src"))

from sentry_satellite import protocol  # noqa: E402

NODE = "zero-entrance"
SOURCE = "pir-1"
START = datetime(2026, 9, 16, 21, 0, 0, tzinfo=UTC)


class Ids:
    """Identifiers that are random-looking but the same every run."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def __call__(self) -> str:
        return str(uuid.UUID(int=self._random.getrandbits(128), version=4))


def scenario_motion(ids: Ids) -> list[dict]:
    """A quiet evening: someone walks past the sensor twice."""
    events = protocol.Events(NODE, ids(), id_factory=ids)
    delivery = protocol.Delivery(connection_id=ids(), hub_epoch=1, grant_id=ids())
    messages = []
    for step, detected in enumerate([True, False, True, False]):
        event = events.event(
            SOURCE,
            "sensor.motion",
            detected,
            occurred_at=START + timedelta(seconds=step * 4),
            clock_status="synced",
        )
        messages.append(protocol.envelope(event, delivery))
    return messages


def scenario_duplicates(ids: Ids) -> list[dict]:
    """The link dropped an acknowledgement, so the same event arrives twice.

    Identical, wrapper included. The hub must admit it once and count the other, without
    treating the second copy as a second person walking past.
    """
    messages = scenario_motion(ids)
    return [messages[0], messages[0], messages[1], messages[1]]


def scenario_bad_clock(ids: Ids) -> list[dict]:
    """A board that has never seen a time server, and one that is an hour into the future."""
    events = protocol.Events(NODE, ids(), id_factory=ids)
    delivery = protocol.Delivery(connection_id=ids(), hub_epoch=1, grant_id=ids())
    return [
        protocol.envelope(
            events.event(SOURCE, "sensor.motion", True, occurred_at=START, clock_status="unsynced"),
            delivery,
        ),
        protocol.envelope(
            events.event(
                SOURCE,
                "sensor.motion",
                False,
                occurred_at=START + timedelta(hours=1),
                clock_status="synced",
            ),
            delivery,
        ),
        protocol.envelope(
            events.event(
                SOURCE,
                "sensor.motion",
                True,
                occurred_at=START - timedelta(days=365),
                clock_status="unknown",
            ),
            delivery,
        ),
    ]


def scenario_backlog(ids: Ids) -> list[dict]:
    """Twenty minutes off the air, then everything at once.

    Each message says how long it waited and that it was replayed. None of it is news, and
    none of it should set off an action twenty minutes late.
    """
    events = protocol.Events(NODE, ids(), id_factory=ids)
    connection = ids()
    grant = ids()
    messages = []
    for step in range(12):
        waited = (12 - step) * 100_000
        event = events.event(
            SOURCE,
            "sensor.motion",
            step % 2 == 0,
            occurred_at=START + timedelta(seconds=step * 10),
            clock_status="synced",
        )
        messages.append(
            protocol.envelope(
                event,
                protocol.Delivery(
                    connection_id=connection,
                    hub_epoch=2,
                    grant_id=grant,
                    queued_ms=waited,
                    replayed=True,
                ),
            )
        )
    return messages


def scenario_reboot(ids: Ids) -> list[dict]:
    """The node restarts: a new boot, and sequence numbers that start again at zero.

    Nothing here may look like an event arriving out of order. A sequence is only
    comparable within the boot it belongs to.
    """
    messages = []
    for boot in range(2):
        events = protocol.Events(NODE, ids(), id_factory=ids)
        delivery = protocol.Delivery(
            connection_id=ids(), hub_epoch=3 + boot, grant_id=ids(), initial_state=True
        )
        for step in range(3):
            event = events.event(
                SOURCE,
                "sensor.motion",
                step % 2 == 0,
                occurred_at=START + timedelta(minutes=boot * 5, seconds=step),
                clock_status="synced",
            )
            messages.append(protocol.envelope(event, delivery))
    return messages


def scenario_noisy(ids: Ids) -> list[dict]:
    """A sensor chattering forty times a second, which is a fault, not a burglary."""
    events = protocol.Events(NODE, ids(), id_factory=ids)
    delivery = protocol.Delivery(connection_id=ids(), hub_epoch=5, grant_id=ids())
    return [
        protocol.envelope(
            events.event(
                SOURCE,
                "sensor.motion",
                step % 2 == 0,
                occurred_at=START + timedelta(milliseconds=step * 25),
                clock_status="synced",
            ),
            delivery,
        )
        for step in range(200)
    ]


def scenario_sensor_fault(ids: Ids) -> list[dict]:
    """A sensor that degrades and then stops answering at all."""
    events = protocol.Events(NODE, ids(), id_factory=ids)
    delivery = protocol.Delivery(connection_id=ids(), hub_epoch=6, grant_id=ids())
    qualities = ["valid", "degraded", "degraded", "unavailable"]
    return [
        protocol.envelope(
            events.event(
                SOURCE,
                "sensor.motion",
                None if quality == "unavailable" else step % 2 == 0,
                occurred_at=START + timedelta(seconds=step * 30),
                clock_status="synced",
                quality=quality,
            ),
            delivery,
        )
        for step, quality in enumerate(qualities)
    ]


SCENARIOS = {
    "motion": scenario_motion,
    "duplicates": scenario_duplicates,
    "bad-clock": scenario_bad_clock,
    "backlog": scenario_backlog,
    "reboot": scenario_reboot,
    "noisy": scenario_noisy,
    "sensor-fault": scenario_sensor_fault,
}


def generate(name: str, seed: int = 20260916) -> list[dict]:
    """Every message of one scenario, identical on every run for the same seed."""
    if name == "all":
        messages = []
        for index, scenario in enumerate(SCENARIOS):
            messages.extend(generate(scenario, seed + index))
        return messages
    return SCENARIOS[name](Ids(seed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="motion", choices=[*SCENARIOS, "all"])
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--out", type=Path, help="write here instead of to standard output")
    parser.add_argument("--list", action="store_true", help="describe the scenarios and stop")
    arguments = parser.parse_args()

    if arguments.list:
        width = max(len(name) for name in SCENARIOS)
        for name, scenario in SCENARIOS.items():
            summary = (scenario.__doc__ or "").strip().splitlines()[0]
            print(f"{name.ljust(width)}  {summary}")
        return 0

    messages = generate(arguments.scenario, arguments.seed)
    lines = "".join(json.dumps(message, separators=(",", ":")) + "\n" for message in messages)
    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(lines, encoding="utf-8")
        print(f"wrote {len(messages)} messages to {arguments.out}")
    else:
        sys.stdout.write(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

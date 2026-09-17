#!/usr/bin/env python3
"""Watch a real board for a minute and judge what it says by the hub's own models.

Everything under `firmware/pico` had, until there was a board, only ever run on a computer.
This is the check that the same code on the chip produces the same thing: it opens the USB
serial port, tells the board what time it is — it has no way of knowing — and hands every
event it publishes to the hub's Pydantic models and to the satellite's schema checker.

    firmware/pico/tools/board_check.py --port /dev/ttyACM0 --events 3

Nothing is written to flash and nothing is configured. The board is asked for readings and
the readings are read; if the port has a MicroPython prompt on it rather than this
firmware, the script says so instead of guessing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTRACTS = REPO / "contracts" / "satellite" / "v1"

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "satellite" / "tests"))

import schema as subset  # noqa: E402  (after the path is set)

from sentry_mode.satellites.protocol import EventEnvelope  # noqa: E402


def read_lines(port, seconds: float):
    """Whatever the board says within the deadline, one line at a time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        raw = port.readline()
        if not raw:
            continue
        yield raw.decode("utf-8", errors="replace").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--events", type=int, default=3, help="how many to judge")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait in total")
    arguments = parser.parse_args(argv)

    try:
        import serial
    except ModuleNotFoundError:
        raise SystemExit("this needs pyserial: pip install pyserial") from None

    schema = json.loads((CONTRACTS / "event.schema.json").read_text())
    with serial.Serial(arguments.port, 115200, timeout=1) as port:
        # The board stamps its readings from what it is told here and from its own monotonic
        # clock; a board that was told nothing publishes nothing, which is the point.
        port.write(f"time {int(time.time() * 1000)}\n".encode())
        port.flush()

        seen: list[dict] = []
        said: list[str] = []
        for line in read_lines(port, arguments.timeout):
            if not line:
                continue
            said.append(line)
            if not line.startswith("{"):
                print(line)
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError as problem:
                raise SystemExit(f"the board wrote something that is not JSON: {problem}") from None
            try:
                envelope = EventEnvelope.model_validate(document)
            except Exception as problem:  # noqa: BLE001 — the message is the whole point
                raise SystemExit(f"the hub would not take this event:\n{problem}") from problem
            try:
                subset.validate(document, schema)
            except subset.Invalid as problem:
                raise SystemExit(f"the published schema refuses it: {problem}") from problem
            seen.append(document)
            event = envelope.event
            print(
                f"ok  {event.source_id} seq={event.sequence} {event.value} {event.unit or ''}"
                f" {event.quality.value} clock={event.clock_status.value} at {event.occurred_at}"
            )
            if len(seen) >= arguments.events:
                break

    if not seen:
        if any(">>>" in line for line in said):
            raise SystemExit("that port has a MicroPython prompt on it, not this firmware")
        raise SystemExit(f"the board said nothing this script could judge in {arguments.timeout}s")

    names = {document["event"]["node_id"] for document in seen}
    boots = {document["event"]["boot_id"] for document in seen}
    if len(names) != 1 or len(boots) != 1:
        raise SystemExit(f"one board, {len(names)} names and {len(boots)} boots")
    sequences = [document["event"]["sequence"] for document in seen]
    if sequences != sorted(set(sequences)):
        raise SystemExit(f"the sequence went backwards or repeated: {sequences}")
    stamped = {document["event"]["clock_status"] for document in seen}
    if stamped != {"synced"}:
        raise SystemExit(f"the board was told the time and still says {sorted(stamped)}")

    print(f"\n{len(seen)} events from {names.pop()}, judged by the hub's models and the schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

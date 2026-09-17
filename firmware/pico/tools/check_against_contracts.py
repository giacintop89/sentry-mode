#!/usr/bin/env python3
"""Hold the firmware's own serializer to the contract, judged by the other two sides.

The C++ under src/protocol was written from the same schema as everything else, and a
serializer checked against the schema it was written from proves very little. So this runs
the firmware's writer for real and hands every line it produces to the hub's Pydantic
models and to the satellite's schema checker — two implementations that have never seen
the C++ and do not agree with it by construction.

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

import schema as subset  # noqa: E402  (after the path is set)

from sentry_mode.satellites.protocol import EventEnvelope  # noqa: E402
from sentry_mode.sources.models import ClockStatus, Quality  # noqa: E402


def emitted(binary: Path) -> list[str]:
    finished = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
    if finished.returncode != 0:
        raise SystemExit(f"{binary.name} refused to write its events:\n{finished.stderr}")
    return [line for line in finished.stdout.splitlines() if line.strip()]


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

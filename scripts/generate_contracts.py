#!/usr/bin/env python3
"""Write the satellite contract files from the models that define them.

The satellite runs on a Zero W without Pydantic, so it validates against JSON schemas
instead. Those schemas are generated here rather than written by hand, so the two sides
of the link cannot drift apart: a test fails if the committed files no longer match.

    python scripts/generate_contracts.py [--check]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sentry_mode.audio.blocks import contract as audio_contract  # noqa: E402
from sentry_mode.satellites.control import MESSAGES, control_schema  # noqa: E402
from sentry_mode.satellites.protocol import contract_schema  # noqa: E402

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts" / "satellite" / "v1"


def files() -> dict[Path, str]:
    written = {
        CONTRACTS / "event.schema.json": json.dumps(contract_schema(), indent=2) + "\n",
        CONTRACTS / "audio.json": json.dumps(audio_contract(), indent=2) + "\n",
    }
    for name in MESSAGES:
        path = CONTRACTS / "control" / f"{name}.schema.json"
        written[path] = json.dumps(control_schema(name), indent=2) + "\n"
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report differences, write nothing")
    arguments = parser.parse_args()

    stale = []
    for path, content in files().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        stale.append(path)
        if not arguments.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    verb = "stale" if arguments.check else "wrote"
    for path in stale:
        print(f"{verb}: {path.relative_to(CONTRACTS.parents[2])}")
    if arguments.check and stale:
        print("run scripts/generate_contracts.py to update them")
        return 1
    if not stale:
        print("contracts are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

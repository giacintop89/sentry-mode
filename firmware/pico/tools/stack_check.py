#!/usr/bin/env python3
"""How deep the deepest frame in this firmware is, and whether that is deep enough to hurt.

The stack on both of these chips is eight kilobytes at the top of memory, with the heap
growing up towards it: a function whose frame does not fit does not fail, it writes into
whatever is below. That is not something a test on a board can catch reliably — it depends
on how far the heap has grown when the deep function happens to be called — so it is caught
here instead, at build time, from the `.su` files `-fstack-usage` leaves beside the objects.

Only this firmware's own functions are counted. The SDK, lwIP and mbedTLS have frames of
their own and are not ours to change; what is ours is that a structure nobody looked at
does not become a frame four times the size of the stack.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Room for the deepest frame here, with the rest of the stack left for the call above it
# and for whatever the SDK is doing underneath. Nothing in this firmware needs a kilobyte
# of locals: the deepest as this was written is `main` on the two RP2040 builds at 1,112
# bytes — the frame at the bottom of the stack, never nested under anything — and the
# deepest of the rest reads a provisioning record at 848. The two that were larger, a
# command and a source, are each kept in one place instead of built where they are used.
MOST_BYTES = 1536


def frames(build: Path) -> list[tuple[int, str]]:
    """Every function this firmware compiled, deepest first."""
    found: list[tuple[int, str]] = []
    for su in build.rglob("*.su"):
        for line in su.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            where, depth, _kind = parts[0], parts[1], parts[2]
            # `path:line:column:signature`, and a signature can hold colons of its own.
            file = where.split(":", 1)[0]
            if "/firmware/pico/src/" not in file:
                continue
            try:
                found.append((int(depth), where.split(":", 3)[-1]))
            except ValueError:
                continue
    found.sort(reverse=True)
    return found


def main() -> int:
    parse = argparse.ArgumentParser(description=__doc__)
    parse.add_argument("--build-dir", type=Path, required=True)
    parse.add_argument("--most", type=int, default=MOST_BYTES)
    parse.add_argument("--show", type=int, default=3, help="how many of the deepest to print")
    said = parse.parse_args()

    found = frames(said.build_dir)
    if not found:
        print(f"no .su files under {said.build_dir}: was this built with -fstack-usage?")
        return 1
    for depth, what in found[: said.show]:
        print(f"{depth:>6}  {what}")
    deepest, what = found[0]
    if deepest > said.most:
        print(f"\n{what} wants {deepest} bytes of stack, and {said.most} is the most one")
        print("frame here may have: the stack is 8 kB, and what is under it is the heap.")
        return 1
    print(
        f"deepest frame {deepest} bytes of the {said.most} one may have, over {len(found)} "
        f"functions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

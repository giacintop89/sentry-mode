#!/usr/bin/env python3
"""Put the two ends of the USB cable against each other.

The firmware frames by hand, byte by byte, at offsets it decides for itself; the bridge
uses `struct` and `zlib`. Neither has read the other. This takes what the firmware wrote
and reads it with the bridge's own reader and with the published contract, then packs
frames in the bridge and hands them to the firmware's reader — including the ones it should
refuse, because a reader that takes everything is not a reader.

    firmware/pico/tools/link_check.py --build-dir build/pico-host
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTRACTS = REPO / "contracts" / "satellite" / "v1"

sys.path.insert(0, str(REPO / "src"))

from sentry_mode.satellites import link as bridge  # noqa: E402  (after the path is set)


class NotAFrame(Exception):
    """What the firmware wrote is not a frame, which is the finding."""


def tool(build: Path) -> Path:
    found = build / "sentry_link_emit"
    if not found.exists():
        raise SystemExit(f"{found} is not built; cmake --build {build}")
    return found


def emitted(build: Path) -> dict[str, bytes]:
    """Every frame the firmware wrote, by the name it gave it."""
    written = {}
    out = subprocess.run([str(tool(build))], capture_output=True, check=True).stdout
    for line in out.splitlines():
        what, _, hexed = line.decode().partition("\t")
        written[what] = bytes.fromhex(hexed)
    return written


def read_back(build: Path, frames: dict[str, bytes]) -> tuple[dict[str, tuple], dict[str, int]]:
    """What the firmware's own reader made of frames the bridge packed."""
    fed = "\n".join(f"{name}\t{raw.hex()}" for name, raw in frames.items()) + "\n"
    out = subprocess.run(
        [str(tool(build)), "--read"], input=fed.encode(), capture_output=True, check=True
    ).stdout
    heard: dict[str, tuple] = {}
    counts: dict[str, int] = {}
    for line in out.decode().splitlines():
        if line.startswith("#\t"):
            counts = dict(
                (piece.split("=")[0], int(piece.split("=")[1])) for piece in line.split("\t")[1:]
            )
            continue
        name, carries, counter, payload = line.split("\t", 3)
        heard[name] = (carries, int(counter), payload)
    return heard, counts


def by_the_contract(raw: bytes) -> tuple[int, int, bytes]:
    """Unpack one frame using only what the contract file says, and nothing else."""
    said = json.loads((CONTRACTS / "bridge.json").read_text())
    header = struct.Struct(said["struct"])
    trailer = struct.Struct(said["trailer_struct"])
    if header.size != said["header_bytes"] or trailer.size != said["trailer_bytes"]:
        raise NotAFrame("the contract does not agree with itself about its own sizes")
    magic, version, carries, flags, counter, length = header.unpack_from(raw)
    if magic.decode() != said["magic"]:
        raise NotAFrame(f"a frame beginning {magic!r}")
    if version != said["version"] or flags:
        raise NotAFrame(f"version {version}, flags {flags}")
    if length > said["max_payload_bytes"]:
        raise NotAFrame(f"a payload of {length} bytes")
    if len(raw) != header.size + length + trailer.size:
        raise NotAFrame(f"{len(raw)} bytes holding a payload of {length}")
    (checked,) = trailer.unpack_from(raw, header.size + length)
    if checked != zlib.crc32(raw[: header.size + length]) & 0xFFFFFFFF:
        raise NotAFrame("a checksum that is not the checksum of what it covers")
    return carries, counter, raw[header.size : header.size + length]


def check_what_the_firmware_wrote(build: Path) -> int:
    written = emitted(build)
    wanted = {"state", "event", "health", "ack", "empty", "longest", "top-counter"}
    missing = wanted - set(written)
    if missing:
        raise SystemExit(f"the firmware wrote nothing for {sorted(missing)}")

    # The one at the top of the counter's range is read on its own: after a run numbered
    # from zero it would be a gap of four billion, which is arithmetic rather than a cable.
    top = written.pop("top-counter")
    _, at_the_top, _ = by_the_contract(top)
    alone = bridge.Reader(expect_from_the_node=True)
    if at_the_top != 0xFFFFFFFF or alone.feed(top)[0].counter != 0xFFFFFFFF:
        raise SystemExit("the counter at the top of its range did not survive the crossing")
    frames = written

    # One at a time through the contract, then all of them through the bridge's reader in
    # the order they were written, a byte at a time — which is how they arrive on a cable.
    reader = bridge.Reader(expect_from_the_node=True)
    for name, raw in frames.items():
        carries, counter, payload = by_the_contract(raw)
        if carries not in said_from_the_node():
            raise SystemExit(f"{name}: a {carries} frame is not one a node sends")
        got = reader.feed(raw)
        if len(got) != 1:
            raise SystemExit(f"{name}: the bridge made {len(got)} frames of one")
        if (got[0].counter, got[0].payload) != (counter, payload):
            raise SystemExit(f"{name}: the two readers disagree about what is in it")
    if reader.discarded:
        raise SystemExit(f"the bridge threw away {reader.discarded} bytes of what was written")

    # And the same bytes again, one at a time and split anywhere, because a cable does not
    # deliver a frame at a time.
    joined = b"".join(frames.values())
    slowly = bridge.Reader(expect_from_the_node=True)
    out = [one for byte in joined for one in slowly.feed(bytes([byte]))]
    if len(out) != len(frames):
        raise SystemExit(f"{len(out)} frames when fed a byte at a time, not {len(frames)}")
    if slowly.missed:
        raise SystemExit(f"the counters say {slowly.missed} frames went missing")

    longest = frames["longest"]
    if len(longest) != bridge.HEADER.size + bridge.MAX_PAYLOAD + bridge.TRAILER.size:
        raise SystemExit("the longest frame is not as long as the format says it may be")
    print(f"{len(frames) + 1} frames written by the firmware, read by the contract and by")
    print("the bridge, including the largest one and a counter at the top of its range")
    print(f"{len(joined)} bytes, fed whole and a byte at a time, with nothing thrown away")
    return 0


def said_from_the_node() -> set[int]:
    return set(json.loads((CONTRACTS / "bridge.json").read_text())["from_the_node"])


def check_what_the_firmware_reads(build: Path) -> int:
    """Frames packed by the bridge, and what the board makes of them."""
    packed = {
        "hello": bridge.pack(bridge.Frame(bridge.Carries.HELLO, 0, b"")),
        "time": bridge.pack(bridge.Frame(bridge.Carries.TIME, 1, b'{"unix_ms":1789000000000}')),
        "command": bridge.pack(
            bridge.Frame(bridge.Carries.COMMANDS, 2, b'{"schema_version":1,"action":"stop"}')
        ),
        # The board must refuse this one: only a bridge sends commands, so a frame of
        # events arriving from the bridge is a bridge doing something it never does.
        "event-from-the-bridge": bridge.pack(bridge.Frame(bridge.Carries.EVENTS, 3, b"{}")),
    }
    # And one that arrived damaged, which has to cost that frame and nothing after it.
    broken = bytearray(bridge.pack(bridge.Frame(bridge.Carries.COMMANDS, 4, b'{"a":1}')))
    broken[bridge.HEADER.size + 2] ^= 0xFF
    packed["damaged"] = bytes(broken)
    packed["after-the-damaged-one"] = bridge.pack(
        bridge.Frame(bridge.Carries.COMMANDS, 5, b'{"after":true}')
    )

    heard, counts = read_back(build, packed)
    expected = {
        "hello": ("hello", 0, ""),
        "time": ("time", 1, '{"unix_ms":1789000000000}'),
        "command": ("commands", 2, '{"schema_version":1,"action":"stop"}'),
        "event-from-the-bridge": ("refused", 0, ""),
        "damaged": ("refused", 0, ""),
        "after-the-damaged-one": ("commands", 5, '{"after":true}'),
    }
    for name, want in expected.items():
        if heard.get(name) != want:
            raise SystemExit(f"{name}: the firmware read {heard.get(name)}, not {want}")
    if counts.get("refused") != 1:
        raise SystemExit(f"the firmware refused {counts.get('refused')} frames, not 1")
    if not counts.get("discarded"):
        raise SystemExit("the damaged frame was not thrown away")
    print(f"{len(expected)} frames packed by the bridge, read or refused by the firmware:")
    print("a frame from the wrong side and a damaged one cost themselves and nothing after")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", default="build/pico-host", type=Path)
    arguments = parser.parse_args()
    build = arguments.build_dir.resolve()
    try:
        return check_what_the_firmware_wrote(build) or check_what_the_firmware_reads(build)
    except NotAFrame as problem:
        raise SystemExit(f"the firmware wrote something that is not a frame: {problem}") from None


if __name__ == "__main__":
    raise SystemExit(main())

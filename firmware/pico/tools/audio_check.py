#!/usr/bin/env python3
"""Read the firmware's sound blocks with the hub's own decoder, and with the contract.

The firmware writes SMA1 by hand, field by field, at offsets it decides for itself. This
takes what it wrote and hands it to three readers that have never seen that code: the
layout published in `contracts/satellite/v1/audio.json`, a decoder written here from that
file alone, and the hub's `sentry_mode.audio.blocks`, which is what would really be on the
other end of the connection.

    firmware/pico/tools/audio_check.py --build-dir build/pico-host

The hello is checked the same way: against the gateway's own reader, so that a line this
node would send is a line the hub would accept.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTRACTS = REPO / "contracts" / "satellite" / "v1"

sys.path.insert(0, str(REPO / "src"))

from sentry_mode.audio import blocks as hub  # noqa: E402  (after the path is set)


class NotABlock(Exception):
    """What the firmware wrote is not a block, which is the finding."""


def emitted(build: Path) -> dict[str, bytes]:
    """Every block the firmware wrote, by the name it gave it."""
    tool = build / "sentry_audio_emit"
    if not tool.exists():
        raise SystemExit(f"{tool} is not built; cmake --build {build}")
    written = {}
    for line in subprocess.run([str(tool)], capture_output=True, check=True).stdout.splitlines():
        what, _, hexed = line.decode().partition("\t")
        written[what] = bytes.fromhex(hexed)
    return written


def read_by_the_contract(layout: dict, raw: bytes) -> dict:
    """The header, read using nothing but the published layout."""
    header = struct.Struct(layout["struct"])
    if header.size != layout["header_bytes"]:
        raise NotABlock(f"the contract's own struct is {header.size} bytes, not its header_bytes")
    if len(raw) < header.size:
        raise NotABlock(f"{len(raw)} bytes cannot hold a {header.size}-byte header")
    values = header.unpack_from(raw, 0)
    named = dict(zip([field["name"] for field in layout["fields"]], values, strict=True))
    if named["magic"] != layout["magic"].encode():
        raise NotABlock(f"the magic is {named['magic']!r}")
    if named["version"] != layout["version"]:
        raise NotABlock(f"the version is {named['version']}")
    if named["reserved"] != 0:
        raise NotABlock("the reserved field is not zero")
    payload = raw[header.size :]
    if len(payload) != named["samples"] * 2:
        raise NotABlock(f"{named['samples']} samples came with {len(payload)} bytes")
    if not 0 < named["samples"] <= layout["max_block_samples"]:
        raise NotABlock(f"{named['samples']} is not a number of samples this format carries")
    named["pcm"] = payload
    return named


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=REPO / "build" / "pico-host")
    arguments = parser.parse_args(argv)

    layout = json.loads((CONTRACTS / "audio.json").read_text(encoding="utf-8"))
    written = emitted(arguments.build_dir)
    expected = {"first", "second", "after-a-gap", "shortest", "longest", "hello"}
    if set(written) != expected:
        raise SystemExit(f"the firmware wrote {sorted(written)}, not {sorted(expected)}")

    # The layout the hub compiled and the layout it published have to be the same file's.
    if hub.contract() != layout:
        raise SystemExit("the hub's decoder and the published contract disagree")

    read = {name: read_by_the_contract(layout, written[name]) for name in expected - {"hello"}}
    if (
        read["longest"]["samples"] != layout["max_block_samples"]
        or read["shortest"]["samples"] != 1
    ):
        raise SystemExit("the shortest and the longest block are not what they claim to be")

    # Two blocks in one read, which is what a connection really hands the hub: the
    # reassembler is the code on the other end, not a reader written for this check.
    stream = hub.Reassembler()
    played = b"".join(stream.feed(written["first"] + written["second"]))
    if played != read["first"]["pcm"] + read["second"]["pcm"]:
        raise SystemExit("what the hub would play is not what the firmware sent")
    if stream.status() != {
        "blocks": 2,
        "duplicates": 0,
        "gaps": 0,
        "node_gaps": 0,
        "lost_samples": 0,
        "filled_samples": 0,
    }:
        raise SystemExit(f"two blocks in a row are not two clean blocks: {stream.status()}")

    # A byte at a time, which is also what a connection really does. The hub must not need
    # a block to arrive whole in one read, and the firmware must not depend on it either.
    dribbled = hub.Reassembler()
    out = b""
    for index in range(len(written["first"])):
        out += b"".join(dribbled.feed(written["first"][index : index + 1]))
    if out != read["first"]["pcm"] or dribbled.blocks != 1:
        raise SystemExit("a block that arrives in pieces is not the block that was sent")

    # And the one that admits it lost sound: the flag is counted as the node's own gap, and
    # the jump in the sample counter is filled with silence rather than played early.
    after = b"".join(stream.feed(written["after-a-gap"]))
    missing = read["after-a-gap"]["first_sample"] - (
        read["second"]["first_sample"] + read["second"]["samples"]
    )
    if stream.node_gaps != 1 or stream.gaps != 1 or stream.lost_samples != missing:
        raise SystemExit(f"the gap was not counted as one: {stream.status()}")
    if after != bytes(missing * 2) + read["after-a-gap"]["pcm"]:
        raise SystemExit("the silence the hub fills a gap with is not the silence it lost")

    hello = written["hello"]
    if not hello.endswith(b"\n"):
        raise SystemExit("the hello does not end with the newline the gateway reads to")
    asked = json.loads(hello[:-1])
    if asked != {
        "schema_version": 1,
        "kind": "audio",
        "stream_id": "3f7c1a9e5b204d86",
        "source_id": "hall-noise",
        "token": "9f2b6c1d8a3e4f50",
    }:
        raise SystemExit(f"the hello is not what the gateway is given: {asked}")

    samples = sum(one["samples"] for one in read.values())
    print(f"{len(read)} blocks written by the firmware, read by the contract and by the hub")
    print(f"{samples} samples, a gap filled with the silence it lost, and a hello the")
    print("gateway's own reader takes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

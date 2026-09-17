#!/usr/bin/env python3
"""Write a provisioning record the firmware can boot from, and check that it does.

The record is a JSON identity inside the framing `include/sentry/store.h` describes. This
script implements that framing a second time, from the header layout rather than from the
C++, and then asks the firmware what it read back. A header both sides agree on is worth
having before a board is involved; a header only one side has ever parsed is not.

    firmware/pico/tools/pack_provisioning.py --build-dir build/pico-host --verify

What this does not do is provision a board. Certificates, the private key, the flash
offsets and the USB recovery path are the hardware half of PICO-02 and need a Pico.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import zlib
from pathlib import Path

MAGIC = b"SENT"
VERSION = 1
HEADER_BYTES = 20
MAX_RECORD_BYTES = 4096


def frame(payload: bytes, sequence: int) -> bytes:
    """One record: the header, the payload, and a checksum over both."""
    if len(payload) > MAX_RECORD_BYTES:
        raise ValueError(f"{len(payload)} bytes is more than a slot holds")
    header = (
        MAGIC
        + VERSION.to_bytes(2, "little")
        + b"\x00\x00"
        + len(payload).to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
    )
    checksum = zlib.crc32(header + payload) & 0xFFFFFFFF
    return header[:16] + checksum.to_bytes(4, "little") + payload


def identity(node_id: str, host: str, port: int) -> bytes:
    return json.dumps(
        {"node_id": node_id, "mqtt_host": host, "mqtt_port": port},
        separators=(",", ":"),
    ).encode()


def read_back(binary: Path, first: Path, second: Path) -> dict:
    finished = subprocess.run(
        [str(binary), str(first), str(second)], capture_output=True, text=True, check=False
    )
    if finished.returncode != 0:
        raise SystemExit(f"the firmware could not read the slots:\n{finished.stderr}")
    return json.loads(finished.stdout)


def verify(binary: Path) -> None:
    with tempfile.TemporaryDirectory() as where:
        first = Path(where) / "slot-a.bin"
        second = Path(where) / "slot-b.bin"

        # What the firmware should boot from: the newer of two whole records.
        first.write_bytes(frame(identity("pico-ingresso", "hub.lan", 8883), 4))
        second.write_bytes(frame(identity("pico-ingresso", "192.168.11.240", 8883), 5))
        found = read_back(binary, first, second)
        if found != {
            "slot": "second",
            "sequence": 5,
            "node_id": "pico-ingresso",
            "mqtt_host": "192.168.11.240",
            "mqtt_port": 8883,
        }:
            raise SystemExit(f"the firmware read something else: {found}")

        # The power went out partway through writing the newer slot.
        whole = frame(identity("pico-ingresso", "192.168.11.240", 8883), 5)
        second.write_bytes(whole[:-3])
        fallen_back = read_back(binary, first, second)
        if fallen_back["slot"] != "first" or fallen_back["mqtt_host"] != "hub.lan":
            raise SystemExit(f"an interrupted write took the node with it: {fallen_back}")

        # A board that has never been provisioned says so rather than guessing.
        first.write_bytes(b"\xff" * 64)
        second.write_bytes(b"\xff" * 64)
        empty = read_back(binary, first, second)
        if empty != {"slot": "neither"}:
            raise SystemExit(f"an unprovisioned board claimed an identity: {empty}")

    print("the firmware read back every record this script wrote, and refused the torn one")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", default="pico-ingresso")
    parser.add_argument("--mqtt-host", default="hub.lan")
    parser.add_argument("--mqtt-port", type=int, default=8883)
    parser.add_argument("--sequence", type=int, default=1)
    parser.add_argument("--out", type=Path, help="where to write the record")
    parser.add_argument(
        "--build-dir", type=Path, default=Path("build/pico-host"), help="where sentry_unpack is"
    )
    parser.add_argument(
        "--verify", action="store_true", help="have the firmware read back what this wrote"
    )
    arguments = parser.parse_args()

    if arguments.out is not None:
        record = frame(
            identity(arguments.node_id, arguments.mqtt_host, arguments.mqtt_port),
            arguments.sequence,
        )
        arguments.out.write_bytes(record)
        print(f"{len(record)} bytes written to {arguments.out}")

    if arguments.verify:
        binary = arguments.build_dir / "sentry_unpack"
        if not binary.exists():
            raise SystemExit(
                f"{binary} is not there. Build it first:\n"
                f"  cmake -S firmware/pico -B {arguments.build_dir} -G Ninja\n"
                f"  cmake --build {arguments.build_dir}"
            )
        verify(binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

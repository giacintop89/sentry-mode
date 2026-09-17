#!/usr/bin/env python3
"""What a built image may contain, and what it may not.

`T01` asks for the build products of every target — the UF2, the ELF and the map — and for
none of them to carry a secret. This firmware has no secret compiled into it by design: a
board is provisioned over its cable and writes what it was told into its own flash, so the
image is the same on every board. That is a claim worth checking rather than repeating.

What is refused:

  * a PEM block with a body. The PEM labels are in the image on purpose — `credentials.cpp`
    compares against them — but a label followed by base64 would be somebody's certificate
    or key sitting in the firmware.
  * anything from a provisioning record, when one is named with `--provisioning`. That is
    the local check: the network name, the passphrase, the node id and the broker's address
    must not appear in an image built from this tree.

It also prints what was built, because a build product nobody looked at is not evidence.
"""

import argparse
import json
import pathlib
import re
import sys

# A label, then whitespace, then the beginning of a base64 body. Forty characters is well
# past anything that happens by accident and well short of the shortest real key.
PEM_WITH_A_BODY = re.compile(rb"-----BEGIN [A-Z ]{2,40}-----[\r\n]+[A-Za-z0-9+/]{40}")


def sizes(build: pathlib.Path) -> list[tuple[str, int]]:
    found = []
    for name in ("sentry_firmware.uf2", "sentry_firmware.elf", "sentry_firmware.elf.map"):
        path = build / name
        if not path.exists():
            raise SystemExit(f"{path} is not there: the device build did not finish")
        found.append((name, path.stat().st_size))
    return found


def secrets_of(record: pathlib.Path) -> list[tuple[str, str]]:
    """The values in a provisioning record, as (what it is, the text) pairs."""
    document = json.loads(record.read_text())
    wanted = []
    for field in ("node_id", "mqtt_host", "wifi_ssid", "wifi_password"):
        value = document.get(field)
        if isinstance(value, str) and value:
            wanted.append((field, value))
    return wanted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=pathlib.Path)
    parser.add_argument(
        "--provisioning",
        type=pathlib.Path,
        help="a provisioning record whose values must not be in the image",
    )
    arguments = parser.parse_args()

    built = sizes(arguments.build_dir)
    for name, size in built:
        print(f"{name}: {size} bytes")

    problems = []
    for name, _ in built:
        bytes_of_it = (arguments.build_dir / name).read_bytes()
        if PEM_WITH_A_BODY.search(bytes_of_it):
            problems.append(f"{name} carries a PEM block with a body in it")
        if arguments.provisioning is not None and arguments.provisioning.exists():
            for field, value in secrets_of(arguments.provisioning):
                if value.encode() in bytes_of_it:
                    # The value itself is never printed, whatever it was.
                    problems.append(f"{name} carries the {field} from {arguments.provisioning}")

    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    said = "no PEM body"
    if arguments.provisioning is not None and arguments.provisioning.exists():
        said += ", nothing from the provisioning record"
    print(f"{said}: this image is the same on every board")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

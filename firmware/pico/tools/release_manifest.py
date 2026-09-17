#!/usr/bin/env python3
"""What one build of this firmware is, written down so that a board can be identified.

A `.uf2` on somebody's desk says nothing about itself. This writes the manifest that goes
with it: the commit it was built from, the SDK and the submodules underneath it, the hash
of the image, which half of the firmware is actually in it, what the hub will let a node
of that kind be asked for, the pins the board keeps for itself, and how far that board and
each capability have really been taken.

    firmware/pico/tools/release_manifest.py --build-dir build/pico2_w

Almost all of it is measured rather than declared. What cannot be measured — whether a
board has ever been run, what a release does not do — lives in `firmware/pico/release.json`
and is merged in here, so that the claims are in one file somebody can read in a minute.

The measured half is also checked against the declared one on the way past: an image built
for a board with no radio that the hub would offer Bluetooth to, or a hub that would send
more sources than this firmware plans, is a mistake this refuses to write a manifest for.

There is no timestamp in it, deliberately: two people who build the same tree with the same
SDK should get the same manifest, and be able to say so by comparing one hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PICO = HERE.parent
REPO = PICO.parents[1]

sys.path.insert(0, str(REPO / "src"))

from sentry_mode.satellites import platforms  # noqa: E402  (after the path is set)

SCHEMA_VERSION = 1

# What each of these source files means in an image. The build compiles one of each pair,
# and which one it compiled is a fact about the binary rather than about the tree.
MODULES = {
    "net.cpp.o": ("network", "wifi, over TLS, to the broker"),
    "net_wired.cpp.o": ("network", "none: this board is reached over its USB cable"),
    "ble.cpp.o": ("bluetooth", "a passive scanner on the wireless chip"),
    "ble_none.cpp.o": ("bluetooth", "none: this board has no radio"),
    "bridge.cpp.o": ("bridge", "the USB port, carrying frames and the console inside them"),
    "media.cpp.o": ("media", "a second connection the hub can listen to sound on"),
    "i2s.cpp.o": ("microphone", "an I2S receiver on the PIO"),
    "onewire.cpp.o": ("onewire", "a 1-Wire bus, bit-banged on one pin"),
    "flash_vault.cpp.o": ("vault", "five things kept in flash, two slots each"),
}


class NotABuild(SystemExit):
    """The directory given is not a device build of this firmware, which is the finding."""


def run(command: list[str], where: Path | None = None) -> str:
    done = subprocess.run(command, capture_output=True, text=True, cwd=where)
    if done.returncode != 0:
        return ""
    return done.stdout.strip()


def cache(build: Path) -> dict[str, str]:
    """The build's own answers: which board, which SDK, which compiler."""
    found = build / "CMakeCache.txt"
    if not found.exists():
        raise NotABuild(f"{build} has no CMakeCache.txt; it is not a build directory")
    entries = {}
    for line in found.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_]+):[A-Z]+=(.*)$", line)
        if match:
            entries[match.group(1)] = match.group(2)
    if "PICO_BOARD" not in entries:
        raise NotABuild(f"{build} is a host build; a manifest is for an image on a board")
    return entries


def digest(file: Path) -> dict:
    return {"bytes": file.stat().st_size, "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}


def image(build: Path) -> dict:
    uf2 = build / "sentry_firmware.uf2"
    elf = build / "sentry_firmware.elf"
    if not uf2.exists() or not elf.exists():
        raise NotABuild(f"{build} holds no sentry_firmware.uf2; cmake --build {build}")
    said = {"uf2": digest(uf2), "elf": digest(elf)}
    # What it takes on the part, from the linker's own arithmetic rather than from the
    # file size: a UF2 is padded to whole 256-byte pages and says nothing about RAM.
    sizes = run(["arm-none-eabi-size", str(elf)])
    numbers = sizes.splitlines()[-1].split() if sizes else []
    if len(numbers) >= 4 and numbers[0].isdigit():
        text, data, bss = (int(one) for one in numbers[:3])
        said["flash_bytes"] = text + data
        said["ram_bytes"] = data + bss
    return said


def compiled(build: Path) -> dict[str, str]:
    """Which half of this firmware is in the image, read off the objects that were built."""
    objects = build / "CMakeFiles" / "sentry_firmware.dir" / "src" / "device"
    if not objects.is_dir():
        raise NotABuild(f"{objects} does not exist; this build produced no firmware")
    present = {one.name for one in objects.iterdir()}
    inside = {}
    for name, (what, how) in MODULES.items():
        if name in present:
            inside[what] = how
    return inside


def repository() -> dict:
    commit = run(["git", "rev-parse", "HEAD"], REPO)
    changed = run(["git", "status", "--porcelain"], REPO)
    # Only the firmware and what it is checked against decide whether an image is a commit:
    # somebody's half-written note in docs/ does not change the bytes on the board.
    watched = ("firmware/pico/", "contracts/satellite/", "src/sentry_mode/satellites/")
    dirty = sorted(
        line[3:].strip('"')
        for line in changed.splitlines()
        if line[3:].strip('"').startswith(watched)
    )
    return {"commit": commit or "unknown", "built_from_a_clean_tree": not dirty, "modified": dirty}


def sdk(path: Path, build: Path) -> dict:
    # The version the image was built with rather than the one in that directory today: the
    # SDK writes it into a header, and the header is part of what was compiled.
    version = "unknown"
    said = build / "generated" / "pico_base" / "pico" / "version.h"
    if said.exists():
        found = re.search(r'PICO_SDK_VERSION_STRING\s+"([^"]+)"', said.read_text())
        if found:
            version = found.group(1)
    under = {}
    for line in run(["git", "submodule", "status"], path).splitlines():
        parts = line.split()
        if len(parts) >= 2:
            under[Path(parts[1]).name] = parts[0].lstrip("-+U")
    return {
        "version": version,
        "commit": run(["git", "rev-parse", "HEAD"], path) or "unknown",
        "submodules": under,
    }


def board_facts(host: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """What the firmware says about each board and about its own limits."""
    tool = host / "sentry_board_emit"
    if not tool.exists():
        raise NotABuild(
            f"{tool} is not built; cmake --build {host} --target sentry_board_emit, "
            "because the pins and the limits in a manifest are the firmware's answer"
        )
    boards: dict[str, dict] = {}
    limits: dict[str, str] = {}
    for line in run([str(tool)]).splitlines():
        parts = line.split("\t")
        fields = dict(one.split("=", 1) for one in parts[2:] if "=" in one)
        if parts[0] == "board":
            boards[parts[1]] = fields
        elif parts[0] == "limits":
            limits = dict(one.split("=", 1) for one in parts[1:] if "=" in one)
    return boards, limits


def capabilities(chosen: platforms.Platform) -> dict:
    """What the hub will let a node of this kind be asked for, from its own catalogue."""
    return {
        "platform": chosen.name,
        "drivers": list(chosen.drivers),
        "streams": list(chosen.streams),
        "max_sources": chosen.max_sources,
        "max_config_bytes": chosen.max_config_bytes,
        "manual_tests": chosen.manual_tests,
        "experimental": chosen.experimental,
    }


def agrees(board: str, inside: dict, said: dict, limits: dict, chosen: platforms.Platform) -> None:
    """Refuse to write a manifest whose halves say different things."""
    radio = said.get("radio") == "yes"
    networked = inside.get("network", "").startswith("wifi")
    if radio != networked:
        raise SystemExit(
            f"{board}: the firmware says radio={said.get('radio')} and the image compiled "
            f"{inside.get('network')}"
        )
    if ("ble" in chosen.drivers) != radio:
        raise SystemExit(
            f"{board}: the hub offers {chosen.name} the drivers {chosen.drivers}, on a board "
            f"whose radio is {said.get('radio')}"
        )
    if chosen.streams and not radio:
        raise SystemExit(
            f"{board}: the hub would ask {chosen.name} for {chosen.streams}, and a board with "
            "no radio has no second connection to send it on"
        )
    planned = int(limits.get("sources", "0"))
    if chosen.max_sources > planned:
        raise SystemExit(
            f"{board}: the hub allows {chosen.max_sources} sources; this firmware plans {planned}"
        )
    held = int(limits.get("configuration", "0"))
    if chosen.max_config_bytes > held:
        raise SystemExit(
            f"{board}: the hub allows a configuration of {chosen.max_config_bytes} bytes; "
            f"a slot on this board holds {held}"
        )


def checks(host: Path) -> dict:
    """Run what can be run without a board, and record what it answered."""
    suites = subprocess.run(
        ["ctest", "--test-dir", str(host), "--output-on-failure"], capture_output=True, text=True
    )
    # ctest says "100% tests passed, 0 tests failed out of 23"; the two numbers that mean
    # something are the failures and the total, and a line that is not there at all is not
    # read as a success.
    counted = re.search(r"(\d+) tests failed out of (\d+)", suites.stdout)
    failed = int(counted.group(1)) if counted else -1
    total = int(counted.group(2)) if counted else 0
    answered = {"suites": {"passed": total - max(failed, 0), "failed": failed}}
    crossed = {}
    for name in ("check_against_contracts", "mqtt_check", "audio_check", "link_check"):
        done = subprocess.run(
            [sys.executable, str(HERE / f"{name}.py"), "--build-dir", str(host)],
            capture_output=True,
            text=True,
        )
        crossed[name] = "passed" if done.returncode == 0 else "failed"
    done = subprocess.run(
        [sys.executable, str(HERE / "pack_provisioning.py"), "--build-dir", str(host), "--verify"],
        capture_output=True,
        text=True,
    )
    crossed["pack_provisioning"] = "passed" if done.returncode == 0 else "failed"
    answered["cross_checks"] = crossed
    return answered


def manifest(build: Path, host: Path, run_checks: bool) -> dict:
    entries = cache(build)
    board = entries["PICO_BOARD"]
    declared = json.loads((PICO / "release.json").read_text())
    known = declared["boards"].get(board)
    if known is None:
        raise SystemExit(f"{board} is not a board firmware/pico/release.json says anything about")
    chosen = platforms.CATALOGUE.get(known["platform"])
    if chosen is None:
        raise SystemExit(f"{known['platform']} is not a platform the hub has in its catalogue")
    boards, limits = board_facts(host)
    said = boards.get(board, {})
    inside = compiled(build)
    agrees(board, inside, said, limits, chosen)
    written = {
        "schema_version": SCHEMA_VERSION,
        "board": board,
        "architecture": chosen.architecture,
        "state": known["state"],
        "note": known["note"],
        "evidence": declared["evidence"],
        "repository": repository(),
        "sdk": sdk(Path(entries.get("PICO_SDK_PATH", "")), build),
        "toolchain": {
            "compiler": run([entries.get("CMAKE_C_COMPILER", "true"), "--version"]).splitlines()[0]
            if entries.get("CMAKE_C_COMPILER")
            else "unknown",
            "platform": entries.get("PICO_PLATFORM", "unknown"),
        },
        "image": image(build),
        "compiled": inside,
        "capabilities": capabilities(chosen),
        "pins": {
            "gpio": said.get("gpio", "unknown"),
            "reserved": [int(one) for one in said.get("reserved", "").split(",") if one],
            "adc": limits.get("adc", "unknown"),
            "pins_that_can_be_claimed_at_once": int(said.get("claims", 0)),
        },
        "firmware_limits": {
            "sources": int(limits.get("sources", 0)),
            "options_per_source": int(limits.get("options", 0)),
            "configuration_bytes": int(limits.get("configuration", 0)),
            # The largest thing this firmware holds at once, and the number to look at
            # first on a part with 264 kB: the wire grammar takes more sources than this
            # board plans, and a parsed command is that grammar's whole shape. Measured by
            # the host build, where the pointers in it are twice the width they are on
            # either chip, so the board's own figure is smaller than this one.
            "parsed_command_bytes_on_the_host": int(limits.get("command_on_the_host", 0)),
        },
        "capability_states": declared["capabilities"],
        "limitations": declared["limitations"],
        "checks": checks(host) if run_checks else None,
    }
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", default="build/pico2_w", type=Path)
    parser.add_argument("--host-dir", default="build/pico-host", type=Path)
    parser.add_argument("--out", type=Path, help="where to write it; default is beside the image")
    parser.add_argument(
        "--no-checks",
        action="store_true",
        help="skip the host suites; the manifest then says nothing about them rather than passing",
    )
    arguments = parser.parse_args()
    written = manifest(
        arguments.build_dir.resolve(), arguments.host_dir.resolve(), not arguments.no_checks
    )
    where = arguments.out or arguments.build_dir / "manifest.json"
    where.write_text(json.dumps(written, indent=2, sort_keys=False) + "\n")
    print(
        f"{where}: {written['board']} as {written['capabilities']['platform']}, {written['state']}"
    )
    print(f"  {written['image']['uf2']['bytes']} bytes, sha256 {written['image']['uf2']['sha256']}")
    print(f"  from {written['repository']['commit'][:12]}", end="")
    print("" if written["repository"]["built_from_a_clean_tree"] else " (a modified tree)")
    print(f"  sdk {written['sdk']['version']}, {written['toolchain']['platform']}")
    if written["checks"] is not None:
        suites = written["checks"]["suites"]
        failed = [
            name for name, how in written["checks"]["cross_checks"].items() if how != "passed"
        ]
        print(f"  {suites['passed']} suites passed, {suites['failed']} failed", end="")
        print(f", cross-checks: {'all passed' if not failed else 'failed ' + ', '.join(failed)}")
        if suites["failed"] or failed:
            raise SystemExit("the manifest records a build whose own checks did not pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""What a release of this agent is: a fixed set of files, named by their checksums.

A board that has to be recovered at two in the morning is not the place to find out what
was installed on it. A release is therefore a plain tar of the source tree with a
manifest beside it, the manifest is the list of every file and its SHA-256, and the whole
thing is reproducible: build it twice from the same tree and the bytes are identical, so
two boards holding the same release id really are running the same code.

It carries no configuration and no identity. Those live under `/etc` and `/var/lib`,
which an install never writes over; that is what makes a rollback to the release before
this one safe, and what makes a backup of a board small enough to keep.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

from sentry_satellite import __version__

NAME = "sentry-satellite"
MANIFEST = "manifest.json"
SCHEMA_VERSION = 1
PACKAGED = ("src", "systemd", "config", "scripts", "pyproject.toml", "README.md")
"""What a release holds: the code, the unit, the example configurations, the scripts that
install and undo it, and the metadata. The scripts travel with the release so that a board
always holds the ones that match what is installed on it."""

IGNORED = ("__pycache__", ".pytest_cache", ".mypy_cache")
CHUNK = 1 << 16


class ReleaseError(RuntimeError):
    """The tree cannot be packaged, or what is installed is not what was packaged."""


def digest(path: Path) -> str:
    total = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            total.update(chunk)
    return total.hexdigest()


def contents(root: Path) -> Iterator[tuple[str, Path]]:
    """Every file a release holds, as the name it will have inside it, in a fixed order."""
    for wanted in PACKAGED:
        start = root / wanted
        if not start.exists():
            raise ReleaseError(f"{start} is missing, so this tree is not a release")
        found = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
        for path in found:
            if any(part in IGNORED for part in path.parts) or path.suffix == ".pyc":
                continue
            yield path.relative_to(root).as_posix(), path


def manifest(root: Path, *, version: str = __version__) -> dict:
    """The list of files and their checksums, with an id of its own over all of them."""
    files = {
        name: {"sha256": digest(path), "bytes": path.stat().st_size}
        for name, path in contents(root)
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "name": NAME,
        "version": version,
        "files": files,
    }
    return {**body, "release_id": release_id(body)}


def release_id(body: dict) -> str:
    """A name for exactly these files: the checksum of the manifest without its own id."""
    without = {key: value for key, value in body.items() if key != "release_id"}
    return hashlib.sha256(canonical(without)).hexdigest()


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def package(root: Path, into: Path, *, version: str = __version__) -> Path:
    """Write `<into>/sentry-satellite-<version>.tar.gz`, the same bytes every time.

    Nothing in the archive carries a time, an owner or a permission bit beyond whether a
    file is executable: a release that changed because it was built on a Tuesday would be
    a release nobody can check.
    """
    listing = manifest(root, version=version)
    into.mkdir(parents=True, exist_ok=True)
    target = into / f"{NAME}-{version}.tar.gz"
    body = canonical({**listing, "files": listing["files"]}) + b"\n"
    with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT, compresslevel=9) as archive:
        archive.addfile(_entry(MANIFEST, len(body)), io.BytesIO(body))
        for name, path in contents(root):
            info = _entry(name, path.stat().st_size, executable=path.stat().st_mode & 0o100)
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    (into / f"{NAME}-{version}.manifest.json").write_bytes(body)
    return target


def _entry(name: str, size: int, *, executable: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if executable else 0o644
    return info


def verify(installed: Path) -> list[str]:
    """What is wrong with an installed release, file by file. Empty means it is intact.

    Extra files are reported too: a release is the whole set, and something dropped into
    it by hand is exactly the surprise this is meant to find.
    """
    listing = installed / MANIFEST
    if not listing.is_file():
        return [f"{listing} is missing, so there is nothing to check this against"]
    try:
        written = json.loads(listing.read_text())
        files = written["files"]
    except (ValueError, KeyError, TypeError) as error:
        return [f"{listing} cannot be read as a manifest: {error}"]
    problems = []
    if written.get("release_id") != release_id(written):
        problems.append(f"{listing} does not match its own release id")
    for name, expected in sorted(files.items()):
        path = installed / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif digest(path) != expected.get("sha256"):
            problems.append(f"{name} is not the file that was packaged")
    here = {name for name, _ in _installed(installed)}
    for name in sorted(here - set(files)):
        problems.append(f"{name} is not part of this release")
    return problems


def _installed(root: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED for part in path.parts):
            continue
        if path.suffix == ".pyc":
            continue
        name = path.relative_to(root).as_posix()
        if name != MANIFEST:
            yield name, path

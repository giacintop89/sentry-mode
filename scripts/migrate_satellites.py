#!/usr/bin/env python3
"""Convert the saved Sentry rules to the second version of the document, with a way back.

Without options nothing is written: the script reads the rules file this node would load
and prints what the conversion would do. The Telegram token is never printed, and no
camera, microphone or broker is opened.

    python scripts/migrate_satellites.py --config config.yaml
    python scripts/migrate_satellites.py --config config.yaml --apply
    python scripts/migrate_satellites.py --config config.yaml --restore BACKUP

`--apply` first copies the file to a private backup directory, next to a manifest holding
its checksum, and only then replaces it. A file already in the second version is left as
it is, so running the script twice changes nothing. `--restore` puts a backup back after
checking its checksum, and keeps a copy of the file it replaces. Stop the node first: a
running node keeps the rules it loaded and would write them back on the next save.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sentry_mode.config import load_config  # noqa: E402
from sentry_mode.sentry.config import SCHEMA_VERSION, VisionTrigger  # noqa: E402
from sentry_mode.sentry.migration import (  # noqa: E402
    LEGACY_SCHEMA,
    read_document,
    write_document,
)

MANIFEST_SUFFIX = ".manifest.json"


class Refused(RuntimeError):
    """The file is not what the script expected, so nothing was changed."""


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    try:
        return read_document(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(f"{path} cannot be read as a rules file: {exc}") from exc


def report(path: Path) -> dict:
    """What this node has and what the conversion would make of it. No secrets."""
    if not path.exists():
        return {
            "rules_file": str(path),
            "exists": False,
            "action": "nothing to convert; the node writes the second version when needed",
        }
    document = load(path)
    config = document.config
    rules = []
    for rule in config.rules:
        trigger = rule.trigger
        rules.append(
            {
                "id": rule.id,
                "name": rule.name,
                "enabled": rule.enabled,
                "trigger": trigger.type,
                "watches": getattr(trigger, "source_id", None),
                "object": trigger.object if isinstance(trigger, VisionTrigger) else None,
                "actions": [action.type for action in rule.actions],
            }
        )
    return {
        "rules_file": str(path),
        "exists": True,
        "schema_version": document.schema_version,
        "revision": document.revision,
        "action": "convert to version 2"
        if document.schema_version == LEGACY_SCHEMA
        else "already version 2; nothing to do",
        "fault_policy": config.fault_policy,
        "test_mode": config.test_mode,
        "telegram_token_configured": bool(config.telegram.bot_token.get_secret_value()),
        "ssh_commands": sorted(config.ssh_commands),
        "rules": rules,
    }


def replace(path: Path, content: str) -> None:
    """Write a file so that a crash leaves either the old one or the new one."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as out:
            name = Path(out.name)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        name.chmod(0o600)
        name.replace(path)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def backup(path: Path, directory: Path, reason: str) -> Path:
    """A private copy of the file, with a manifest that says what it is."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    copy = directory / f"{path.stem}-{stamp}{path.suffix}"
    if copy.exists():
        raise Refused(f"{copy} already exists")
    shutil.copyfile(path, copy)
    copy.chmod(0o600)
    manifest = {
        "original": str(path.resolve()),
        "sha256": checksum(copy),
        "schema_version": load(copy).schema_version,
        "reason": reason,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = copy.with_name(copy.name + MANIFEST_SUFFIX)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return copy


def apply(path: Path, directory: Path) -> dict:
    result = report(path)
    if not result["exists"] or result["schema_version"] == SCHEMA_VERSION:
        return {**result, "changed": False}
    before = checksum(path)
    document = load(path)
    copy = backup(path, directory, "before conversion to version 2")
    if checksum(path) != before:
        raise Refused(f"{path} changed while it was being backed up; nothing was converted")
    body = write_document(document.config, document.revision, SCHEMA_VERSION)
    replace(path, json.dumps(body))
    converted = load(path)
    if converted.schema_version != SCHEMA_VERSION or converted.config != document.config:
        raise Refused(f"the converted file does not read back the same; restore {copy}")
    return {**report(path), "changed": True, "backup": str(copy)}


def restore(path: Path, copy: Path, directory: Path) -> dict:
    manifest_path = copy.with_name(copy.name + MANIFEST_SUFFIX)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{manifest_path} cannot be read: {exc}") from exc
    if checksum(copy) != manifest.get("sha256"):
        raise Refused(f"{copy} does not match its manifest; it was not restored")
    if Path(manifest.get("original", "")) != path.resolve():
        raise Refused(f"{copy} was taken from {manifest.get('original')}, not {path}")
    load(copy)
    kept = backup(path, directory, "before restoring an older copy") if path.exists() else None
    replace(path, copy.read_text(encoding="utf-8"))
    return {
        **report(path),
        "changed": True,
        "restored_from": str(copy),
        "previous_kept_at": str(kept) if kept else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="the node's YAML file (default: SENTRY_MODE_CONFIG)")
    parser.add_argument(
        "--backup-dir", type=Path, help="where copies go (default: next to the rules file)"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="convert the file")
    action.add_argument("--restore", type=Path, metavar="BACKUP", help="put a backup back")
    args = parser.parse_args(argv)

    path = load_config(args.config).sentry_state_file
    directory = args.backup_dir or path.parent / "backups"
    try:
        if args.apply:
            result = apply(path, directory)
        elif args.restore:
            result = restore(path, args.restore, directory)
        else:
            result = {**report(path), "changed": False, "dry_run": True}
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

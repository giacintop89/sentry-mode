"""Sources the hub asked for, kept apart from the file the node was installed with.

The installed file stays read-only and is never rewritten. What the hub sends is checked
against the same rules, narrowed to the drivers this agent has, and kept in the state
directory as an overlay: a revision number and the list of sources that replaces the
installed one. Nothing else in the configuration can be changed this way.

A revision only ever goes up. An overlay that no longer passes the checks, for example
after an upgrade that dropped a driver, is set aside with a warning and the installed list
is used, rather than a node that refuses to start.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from sentry_satellite.config import Config, ConfigError, Source, parse_sources

log = logging.getLogger(__name__)

DEFAULT_OVERLAY = Path("/var/lib/sentry-satellite/sources.json")


class RemoteError(ValueError):
    """The requested configuration cannot be applied, and nothing was changed."""


@dataclass(frozen=True)
class Applied:
    revision: int
    config: Config


def check(
    config: Config, revision: int, entries: list, *, current: int, kinds: tuple[str, ...]
) -> Config:
    """The configuration the request would produce, or the reason it will not."""
    if revision <= current:
        raise RemoteError(f"revision {revision} is not newer than {current}")
    try:
        sources = parse_sources(
            entries,
            node_id=config.node_id,
            profile=config.profile,
            kinds=kinds,
            where="sources",
        )
    except ConfigError as error:
        raise RemoteError(str(error)) from error
    return with_sources(config, sources)


def with_sources(config: Config, sources: tuple[Source, ...]) -> Config:
    return replace(config, sources=sources)


def as_entries(sources: tuple[Source, ...]) -> list[dict]:
    return [{"id": source.id, "kind": source.kind, **source.options} for source in sources]


class Overlay:
    def __init__(self, path: Path = DEFAULT_OVERLAY) -> None:
        self.path = path

    def load(self, config: Config, *, kinds: tuple[str, ...]) -> Applied:
        """The installed configuration with the overlay on top, if there is a usable one."""
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Applied(0, config)
        except (OSError, ValueError) as error:
            log.warning("ignoring the remote configuration in %s: %s", self.path, error)
            return Applied(0, config)
        revision = document.get("revision") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or document.get("node_id") != config.node_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            log.warning("ignoring the remote configuration in %s: not for this node", self.path)
            return Applied(0, config)
        try:
            applied = check(config, revision, document.get("sources"), current=0, kinds=kinds)
        except RemoteError as error:
            log.warning("ignoring remote configuration revision %s: %s", revision, error)
            return Applied(0, config)
        return Applied(revision, applied)

    def save(self, config: Config, revision: int) -> None:
        """Replace the overlay in one step. A crash leaves the old one or the new one."""
        document = {
            "schema_version": 1,
            "node_id": config.node_id,
            "revision": revision,
            "sources": as_entries(config.sources),
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".sources-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

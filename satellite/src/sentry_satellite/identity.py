"""Who this node is, and which run of it this is.

The identity is issued once, during provisioning, and then never changes: it is the name
the hub approved and tied to a certificate. The runtime epoch is the opposite — a fresh
`boot_id` every start, so that the hub can tell a restart from a reconnection and refuse
to mix sequence numbers from two different lives of the same process.
"""

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from sentry_satellite.names import check_name


class IdentityError(RuntimeError):
    """The node has no usable identity, or was asked to overwrite one it already has."""


@dataclass(frozen=True)
class Identity:
    node_id: str
    provisioned_at: str
    boot_id: str

    @property
    def summary(self) -> str:
        return f"{self.node_id} (provisioned {self.provisioned_at}, boot {self.boot_id})"


def new_boot_id() -> str:
    return str(uuid.uuid4())


def load(path: Path) -> Identity:
    """Read the identity written at provisioning, with a new boot id for this run."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise IdentityError(
            f"{path} does not exist: this node has not been provisioned yet"
        ) from error
    except json.JSONDecodeError as error:
        raise IdentityError(f"{path} is not readable as an identity") from error
    try:
        node_id = check_name(stored["node_id"], "a node id")
        provisioned_at = str(stored["provisioned_at"])
    except KeyError as error:
        raise IdentityError(f"{path} is missing {error.args[0]}") from error
    return Identity(node_id=node_id, provisioned_at=provisioned_at, boot_id=new_boot_id())


def create(path: Path, node_id: str, now: str) -> Identity:
    """Write the identity for a node that has just been provisioned.

    Refuses to replace one that already exists. Re-issuing an identity would orphan every
    rule the hub holds against the old name, so it is an administrative act with a new
    certificate, not something a first boot can do by accident.
    """
    check_name(node_id, "a node id")
    if path.exists():
        raise IdentityError(f"{path} already holds an identity; revoke it on the hub instead")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"node_id": node_id, "provisioned_at": now}
    temporary = path.with_name(f".{path.name}.new")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return Identity(node_id=node_id, provisioned_at=now, boot_id=new_boot_id())

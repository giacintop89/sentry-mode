"""Which nodes exist, which are allowed to speak, and which certificate proves it.

A node is registered before it is trusted. The administrator signs its certificate and
approves the record; until then it may connect to the broker and say nothing else. The
record is what binds a certificate to a name: a valid signature from the system's
certificate store is not, on its own, permission to be `zero-entrance`.

This is an administrative record, small and worth reading by eye, so it lives in a JSON
file written atomically with private permissions. The event journal is a different problem
and gets a different store.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sentry_mode.sources.models import NAME

Status = Literal["pending", "approved", "revoked"]

PEM_BODY = re.compile(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL)


class UnknownNode(LookupError):
    """A name arrived that this hub has never been told about."""


class NotApproved(PermissionError):
    """The node is known, and is not allowed to say anything yet."""


class WrongCertificate(PermissionError):
    """The node is known and approved, but this is not the certificate it was approved with."""


def fingerprint(certificate: str | bytes) -> str:
    """The SHA-256 of the certificate itself, which is what actually identifies it.

    A name can be reissued and a serial can be reused by a careless authority. The bytes
    cannot, so the record is pinned to them.
    """
    if isinstance(certificate, bytes):
        try:
            text = certificate.decode("ascii")
        except UnicodeDecodeError:
            return hashlib.sha256(certificate).hexdigest()
    else:
        text = certificate
    match = PEM_BODY.search(text)
    if match is None:
        raise ValueError("that is not a PEM certificate")
    der = base64.b64decode("".join(match.group(1).split()))
    return hashlib.sha256(der).hexdigest()


class NodeRecord(BaseModel):
    """What the hub knows about one satellite, independently of whether it is connected."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=NAME)
    display_name: str = Field(min_length=1, max_length=120)
    profile: str = Field(default="sensor-presence", min_length=1, max_length=40)
    zone: str | None = Field(default=None, pattern=NAME)
    status: Status = "pending"
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    serial: str | None = Field(default=None, max_length=64)
    agent_version: str | None = Field(default=None, max_length=32)
    protocol: int = 5
    registered_at: datetime
    approved_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = Field(default=None, max_length=200)

    @property
    def may_publish(self) -> bool:
        return self.status == "approved"


class NodeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = 0
    hub_epoch: int = 0
    nodes: dict[str, NodeRecord] = Field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


class NodeRegistry:
    """The approved list, and the only thing that turns a connection into a name."""

    def __init__(self, path: Path, *, now=_now) -> None:
        self._path = Path(path)
        self._now = now
        self._lock = RLock()
        self._document = NodeDocument()
        self._read_at: tuple[int, int] | None = None
        self.load()

    # -- persistence -------------------------------------------------------------

    def load(self) -> NodeDocument:
        with self._lock:
            try:
                stamp = self._path.stat()
            except OSError:
                self._read_at = None
                self._document = NodeDocument()
                return self._document
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._document = NodeDocument.model_validate(data)
            self._read_at = (stamp.st_mtime_ns, stamp.st_size)
            return self._document

    def refresh(self) -> None:
        """Take in a registry that was changed underneath us, by `satellite_admin.py`.

        Approving a node is an administrative act with a shell, and a hub that only read
        the file at startup would leave the person who did it looking at a node that never
        appears. The file is written whole and moved into place, so there is no half of one
        to read; when it has not moved, this is one `stat` and nothing else.
        """
        with self._lock:
            try:
                stamp = self._path.stat()
            except OSError:
                if self._read_at is not None:
                    self.load()
                return
            if self._read_at != (stamp.st_mtime_ns, stamp.st_size):
                self.load()

    def _persist(self) -> None:
        """Write the registry out. Callers hold the lock and have refreshed first."""
        self._document.revision += 1
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.new")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(self._document.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def revision(self) -> int:
        with self._lock:
            return self._document.revision

    def next_epoch(self) -> int:
        """The next number this hub will stamp a connection with.

        It is persisted and it only counts up, across restarts and across crashes. An
        epoch that could repeat would let a grant from a previous life of the hub be
        replayed into this one and still look current.
        """
        with self._lock:
            self.refresh()
            self._document.hub_epoch += 1
            self._persist()
            return self._document.hub_epoch

    # -- lifecycle ---------------------------------------------------------------

    def register(
        self,
        node_id: str,
        *,
        display_name: str | None = None,
        profile: str = "sensor-presence",
        zone: str | None = None,
        certificate: str | None = None,
    ) -> NodeRecord:
        """Record a node as waiting. Registering is not approving."""
        with self._lock:
            self.refresh()
            if node_id in self._document.nodes:
                raise ValueError(f"{node_id} is already registered")
            record = NodeRecord(
                node_id=node_id,
                display_name=display_name or node_id,
                profile=profile,
                zone=zone,
                status="pending",
                fingerprint=fingerprint(certificate) if certificate else None,
                registered_at=self._now(),
            )
            self._document.nodes[node_id] = record
            self._persist()
            return record

    def approve(
        self, node_id: str, *, certificate: str | None = None, pinned: str | None = None
    ) -> NodeRecord:
        """Allow a node to publish, tied to the certificate it was approved with.

        Either hand over the certificate, or the fingerprint of it when that is what was
        read off the node. What is not possible is approving a node with nothing: the
        record would then accept whatever turned up under that name.
        """
        with self._lock:
            self.refresh()
            record = self.require(node_id)
            if certificate is not None:
                pinned = fingerprint(certificate)
            pinned = pinned or record.fingerprint
            if pinned is None:
                raise ValueError(
                    f"{node_id} has no certificate on file; approve it with the one it was issued"
                )
            updated = record.model_copy(
                update={
                    "status": "approved",
                    "fingerprint": pinned,
                    "approved_at": self._now(),
                    "revoked_at": None,
                    "revoked_reason": None,
                }
            )
            self._document.nodes[node_id] = updated
            self._persist()
            return updated

    def revoke(self, node_id: str, *, reason: str | None = None) -> NodeRecord:
        """Take permission away. The record stays: history is not a permission."""
        with self._lock:
            self.refresh()
            record = self.require(node_id)
            updated = record.model_copy(
                update={
                    "status": "revoked",
                    "revoked_at": self._now(),
                    "revoked_reason": reason,
                }
            )
            self._document.nodes[node_id] = updated
            self._persist()
            return updated

    def forget(self, node_id: str) -> None:
        with self._lock:
            self.refresh()
            self.require(node_id)
            del self._document.nodes[node_id]
            self._persist()

    def describe(self, node_id: str, /, **changes) -> NodeRecord:
        """Change what a node is called or where it is, never who it is."""
        allowed = {"display_name", "zone", "profile", "agent_version", "serial"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"a node's {', '.join(sorted(unknown))} is not a description")
        with self._lock:
            self.refresh()
            record = self.require(node_id)
            updated = record.model_copy(update=changes)
            self._document.nodes[node_id] = updated
            self._persist()
            return updated

    # -- questions ---------------------------------------------------------------

    def get(self, node_id: str) -> NodeRecord | None:
        with self._lock:
            return self._document.nodes.get(node_id)

    def require(self, node_id: str) -> NodeRecord:
        record = self.get(node_id)
        if record is None:
            raise UnknownNode(node_id)
        return record

    def all(self) -> list[NodeRecord]:
        with self._lock:
            return sorted(self._document.nodes.values(), key=lambda record: record.node_id)

    def of_status(self, status: Status) -> list[NodeRecord]:
        return [record for record in self.all() if record.status == status]

    def authenticate(
        self,
        *,
        node_id: str,
        username: str | None = None,
        client_id: str | None = None,
        certificate_fingerprint: str | None = None,
    ) -> NodeRecord:
        """Decide whether this connection really is the node it says it is.

        Everything has to agree: the name on the topic, the username the broker
        authenticated, the client identifier, and the certificate the record was approved
        with. One of them disagreeing is not a detail to log and carry on from; it is
        either a misconfiguration or somebody trying something.
        """
        record = self.get(node_id)
        if record is None or not record.may_publish:
            # A name nobody has heard of, or one that is not approved yet, is the case
            # where the file on disk may have moved on: look again before refusing.
            self.refresh()
        record = self.require(node_id)
        if not record.may_publish:
            raise NotApproved(f"{node_id} is {record.status}")
        for label, value in (("username", username), ("client id", client_id)):
            if value is not None and value != node_id:
                raise WrongCertificate(f"the {label} {value!r} is not {node_id!r}")
        if record.fingerprint is None:
            raise WrongCertificate(f"{node_id} is approved without a certificate on file")
        if certificate_fingerprint is not None and certificate_fingerprint != record.fingerprint:
            raise WrongCertificate(f"{node_id} presented a certificate it was not approved with")
        return record

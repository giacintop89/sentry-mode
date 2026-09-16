"""Building a message the hub will accept, and nothing else.

The shape is fixed by `contracts/satellite/v1/event.schema.json`, which the hub generates
from its own models. This module is the satellite's half of that agreement: it builds
envelopes, it numbers them, and it refuses to send one that is already wrong.
"""

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Callable

from sentry_satellite.names import InvalidName, check_kind, check_name

SCHEMA_VERSION = 1
TOPIC_PREFIX = "sentry/v1"
EVENT_MAX_BYTES = 8192
"""What the hub refuses to parse. Bigger than any reading, smaller than a picture."""

QUALITIES = ("valid", "degraded", "unavailable", "unknown")
CLOCK_STATUSES = ("synced", "unsynced", "unknown")


class ProtocolError(ValueError):
    """The agent was asked to send something the hub would refuse."""


def topic(node_id: str, channel: str) -> str:
    return f"{TOPIC_PREFIX}/nodes/{node_id}/{channel}"


def timestamp(moment: datetime) -> str:
    """RFC 3339 with the offset spelled out, because a guessed time zone is worthless."""
    if moment.tzinfo is None:
        raise ProtocolError("a reading carries the time zone it was taken in")
    text = moment.astimezone(UTC).isoformat()
    return text.replace("+00:00", "Z")


def _check_value(value: object) -> object:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise ProtocolError("a reading that is not a number is not a reading")
    if value is not None and not isinstance(value, bool | int | float | str):
        raise ProtocolError(f"a reading cannot be a {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Delivery:
    connection_id: str
    hub_epoch: int
    grant_id: str | None = None
    queued_ms: int = 0
    replayed: bool = False
    initial_state: bool = False

    def as_dict(self) -> dict:
        return {
            "connection_id": self.connection_id,
            "hub_epoch": self.hub_epoch,
            "grant_id": self.grant_id,
            "queued_ms": self.queued_ms,
            "replayed": self.replayed,
            "initial_state": self.initial_state,
        }


class Sequencer:
    """One counter per source, per boot, that only ever goes up.

    The hub uses it as a high-water mark, so handing out the same number twice would make
    a real reading look like a duplicate. The lock is there because drivers read on their
    own threads.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._next: dict[str, int] = {}

    def take(self, source_id: str) -> int:
        with self._lock:
            number = self._next.get(source_id, 0)
            self._next[source_id] = number + 1
            return number


class Events:
    """Makes the events for one node, for one life of the process."""

    def __init__(
        self,
        node_id: str,
        boot_id: str,
        sequencer: Sequencer | None = None,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.node_id = check_name(node_id, "a node id")
        self.boot_id = boot_id
        self._sequencer = sequencer or Sequencer()
        self._id_factory = id_factory

    def event(
        self,
        source_id: str,
        kind: str,
        value: object,
        *,
        occurred_at: datetime,
        unit: str | None = None,
        quality: str = "valid",
        clock_status: str = "unknown",
    ) -> dict:
        try:
            check_name(source_id, "a source id")
            check_kind(kind)
        except InvalidName as error:
            raise ProtocolError(str(error)) from error
        if quality not in QUALITIES:
            raise ProtocolError(f"quality must be one of {', '.join(QUALITIES)}")
        if clock_status not in CLOCK_STATUSES:
            raise ProtocolError(f"clock_status must be one of {', '.join(CLOCK_STATUSES)}")
        if unit is not None and len(unit) > 16:
            raise ProtocolError("a unit is a symbol, not a sentence")
        return {
            "event_id": self._id_factory(),
            "node_id": self.node_id,
            "source_id": source_id,
            "boot_id": self.boot_id,
            "sequence": self._sequencer.take(source_id),
            "kind": kind,
            "occurred_at": timestamp(occurred_at),
            "clock_status": clock_status,
            "value": _check_value(value),
            "unit": unit,
            "quality": quality,
        }


def envelope(event: dict, delivery: Delivery) -> dict:
    return {"schema_version": SCHEMA_VERSION, "event": event, "delivery": delivery.as_dict()}


def encode(message: dict, *, limit: int = EVENT_MAX_BYTES) -> bytes:
    """Serialise a message, refusing here what the hub would refuse on arrival."""
    payload = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > limit:
        raise ProtocolError(f"a message of {len(payload)} bytes is over the {limit} byte limit")
    return payload

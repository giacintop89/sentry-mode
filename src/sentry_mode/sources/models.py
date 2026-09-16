"""The names, kinds and health of the things a rule is allowed to talk about.

A source is a single stream of one datum: a camera, a microphone, a passive infrared
sensor, the speaker this node answers through. Local devices and satellite devices are
described the same way so that the rest of the node never has to ask where a reading
came from before it can use it.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$"
"""Lowercase ASCII, digits and hyphens: readable in a log line, safe in an MQTT topic.

The rule is a pattern rather than a validator so that it reaches the generated JSON
schema, where the satellites can hold themselves to exactly the same names.
"""

PRIMARY_CAMERA = "legacy-primary"
PRIMARY_MICROPHONE = "legacy-microphone"
PRIMARY_SPEAKER = "legacy-speaker"
"""This node's own devices. They live here, beside the grammar, so that anything that
needs the names can have them without loading the node's whole configuration."""


class SourceKind(str, Enum):
    """What a source produces, which decides what a rule may ask of it."""

    CAMERA = "camera"
    MICROPHONE = "microphone"
    SPEAKER = "speaker"
    SENSOR = "sensor"
    PRESENCE = "presence"


class SourceState(str, Enum):
    """How the source itself is doing, independently of any single reading."""

    READY = "ready"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    FAILED = "failed"


class Quality(str, Enum):
    """How much a single reading can be trusted."""

    VALID = "valid"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ClockStatus(str, Enum):
    """Whether the timestamp on a reading came from a clock that knows the time."""

    SYNCED = "synced"
    UNSYNCED = "unsynced"
    UNKNOWN = "unknown"


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(Frozen):
    """The permanent name of one source.

    A source on this node is a bare name; a source on a satellite is the node name and
    the source name joined by a dot. The pair never changes once it has been issued:
    renaming a thing is a display change, and moving it to another node makes a new source.
    """

    node: str | None = Field(default=None, pattern=NAME)
    name: str = Field(pattern=NAME)

    @classmethod
    def parse(cls, value: str) -> SourceRef:
        node, dot, name = value.partition(".")
        if not dot:
            return cls(name=node)
        return cls(node=node, name=name)

    @property
    def id(self) -> str:
        return self.name if self.node is None else f"{self.node}.{self.name}"

    @property
    def is_local(self) -> bool:
        return self.node is None

    def __str__(self) -> str:
        return self.id


class SourceRecord(Frozen):
    """Everything the node knows about one source without having to reach it."""

    ref: SourceRef
    kind: SourceKind
    display_name: str = Field(min_length=1, max_length=120)
    zone: str | None = Field(default=None, pattern=NAME)
    origin: Literal["local", "satellite"] = "local"
    state: SourceState = SourceState.READY
    address: int | str | None = None
    unit: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def check_origin_matches_ref(self) -> SourceRecord:
        if self.origin == "local" and not self.ref.is_local:
            raise ValueError("a local source is named without a node prefix")
        if self.origin == "satellite" and self.ref.is_local:
            raise ValueError("a satellite source is named by its node and its source")
        return self

    @property
    def id(self) -> str:
        return self.ref.id

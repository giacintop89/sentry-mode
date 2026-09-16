"""The shape of a message on the wire, shared by the hub and every satellite.

This module is the single authority for version 1 of the event envelope: the JSON schema
under `contracts/satellite/v1/` is generated from these models, and both sides of the
link are tested against the same fixtures. It deliberately depends on nothing but
Pydantic, so that importing it can never pull in the camera or the detector.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from sentry_mode.sources.models import NAME, ClockStatus, Quality, SourceRef

SCHEMA_VERSION: Literal[1] = 1

KIND = r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$"
"""A family and a name, as in `sensor.motion` or `audio.loud_noise`."""

TIMESTAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
"""A moment with the offset it was taken at. A schema cannot require an offset of the
`date-time` format alone, and a reading whose time zone is a guess is worse than useless
when the hub has to order it against everything else, so the rule is written out."""


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SatelliteEvent(Wire):
    """One reading from one source, as the satellite that took it describes it.

    `source_id` is the name the source has on its own node; the hub joins it to `node_id`
    to get the identifier it uses everywhere else. The satellite never has to know that
    identifier, and cannot claim a source on another node.
    """

    event_id: UUID
    node_id: str = Field(pattern=NAME)
    source_id: str = Field(pattern=NAME)
    boot_id: UUID
    sequence: int = Field(ge=0)
    kind: str = Field(pattern=KIND)
    occurred_at: AwareDatetime = Field(json_schema_extra={"pattern": TIMESTAMP})
    clock_status: ClockStatus = ClockStatus.UNKNOWN
    value: bool | int | float | str | None = None
    unit: str | None = Field(default=None, max_length=16)
    quality: Quality = Quality.VALID

    @property
    def ref(self) -> SourceRef:
        return SourceRef(node=self.node_id, name=self.source_id)


class Delivery(Wire):
    """How this copy of the event reached the hub, which is not part of what happened.

    A replayed event and a fresh one describe the same moment; only these fields differ,
    which is what lets the hub tell them apart without rewriting the event itself.
    """

    connection_id: UUID
    hub_epoch: int = Field(ge=0)
    grant_id: UUID | None = None
    queued_ms: int = Field(default=0, ge=0)
    replayed: bool = False
    initial_state: bool = False


class EventEnvelope(Wire):
    """What is actually published: a version, an event, and how it got here."""

    schema_version: Literal[1] = SCHEMA_VERSION
    event: SatelliteEvent
    delivery: Delivery


def contract_schema() -> dict:
    """The JSON schema published to satellites, generated from the models above."""
    schema = EventEnvelope.model_json_schema()
    schema["$id"] = "https://sentry-mode.invalid/contracts/satellite/v1/event.schema.json"
    schema["title"] = "Satellite event envelope, version 1"
    return schema

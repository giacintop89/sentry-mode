"""Settings for the satellite side of the node, off until there is something to talk to."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MqttConfig(Section):
    """Where the broker is and how this node proves who it is to it.

    There is no setting here for skipping verification. The address the satellites use is
    the one the hub's certificate has to be valid for, including when that address is an
    IP; `scripts/satellite_admin.py hub-cert` issues it that way.
    """

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8883, ge=1, le=65535)
    topic_prefix: str = Field(default="sentry/v1", pattern=r"^[a-z0-9]+(?:/[a-z0-9]+)*$")
    keepalive_seconds: int = Field(default=30, ge=5, le=300)
    tls_ca_file: Path = Path("/etc/sentry-mode/satellites/ca.crt")
    tls_cert_file: Path = Path("/etc/sentry-mode/satellites/hub.crt")
    tls_key_file: Path = Path("/etc/sentry-mode/satellites/hub.key")


class HealthConfig(Section):
    """How long silence is tolerated before it is reported as silence."""

    heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    stale_after_seconds: int = Field(default=45, ge=2, le=900)
    offline_after_seconds: int = Field(default=60, ge=3, le=1800)

    @field_validator("offline_after_seconds")
    @classmethod
    def offline_comes_after_stale(cls, value: int, info) -> int:
        stale = info.data.get("stale_after_seconds")
        if stale is not None and value <= stale:
            raise ValueError("a node is stale before it is offline, not after")
        return value


class LimitsConfig(Section):
    """How much one node may send, how much everyone may send, and how long news keeps.

    The two rates are separate on purpose: a node that has gone mad is meant to run out of
    its own budget long before it exhausts the hub's, so the other nodes keep working.
    """

    event_max_bytes: int = Field(default=8192, ge=256, le=65536)
    events_per_minute: int = Field(default=600, ge=1, le=60000)
    total_events_per_minute: int = Field(default=3000, ge=1, le=600000)
    queue_depth: int = Field(default=256, ge=8, le=10000)
    accept_within_seconds: int = Field(default=300, ge=5, le=86400)
    future_tolerance_seconds: int = Field(default=5, ge=0, le=300)
    journal_days: int = Field(default=14, ge=1, le=3650)
    # Receipts outlive the replay window by construction: the shortest retention is a day
    # and the longest window is a day, so no setting can forget a receipt while its event
    # could still arrive and be admitted twice.
    dedup_days: int = Field(default=7, ge=1, le=3650)

    @field_validator("total_events_per_minute")
    @classmethod
    def everyone_is_more_than_anyone(cls, value: int, info) -> int:
        each = info.data.get("events_per_minute")
        if each is not None and value < each:
            raise ValueError("the budget for every node cannot be smaller than one node's")
        return value


class SessionsConfig(Section):
    """How long a node may publish before it has to be told again that it may."""

    grant_seconds: int = Field(default=300, ge=10, le=3600)
    renew_every_seconds: int = Field(default=120, ge=5, le=1800)

    @field_validator("renew_every_seconds")
    @classmethod
    def renewal_comes_before_expiry(cls, value: int, info) -> int:
        granted = info.data.get("grant_seconds")
        if granted is not None and value >= granted:
            raise ValueError("a grant has to be renewed before it runs out")
        return value


class SatellitesConfig(Section):
    """Whether this node listens to other nodes at all, and what it does when one misbehaves.

    Everything the media path needs arrives with the increments that use it. A node with
    `enabled: false` imports none of this, contacts nothing, and behaves exactly as it did
    before there were satellites.
    """

    enabled: bool = False
    store_path: Path = Path(".local/satellites.sqlite3")
    nodes_file: Path = Path(".local/satellite-nodes.json")
    fault_policy: Literal["isolated", "global"] = "isolated"
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)

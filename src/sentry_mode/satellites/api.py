"""What the satellites page shows, and the one change it may ask a node to make.

The page reads one document: every registered node with what it said about itself, its
sources with their latest readings, and what became of the last configuration sent to it.
It is assembled from the service status here so that the service keeps reporting facts
and the page gets names it can show.

A configuration request only carries sources, and only of the kinds a satellite agent can
drive today. The node checks everything again and has the last word; the hub refuses the
obvious early so that a typo is an error on the page, not a round trip over MQTT.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sentry_mode.sources.models import NAME

DRIVERS = ("gpio", "onewire", "bme280", "adc", "csi", "microphone", "ble", "dummy")
"""Kinds the satellite agent has a driver for. Only these can be configured remotely."""

PENDING = {
    "uvc": "arrives once a USB camera is qualified on the board",
}

MAX_SOURCES = 32
Scalar = str | int | float | bool


class SourceRequest(BaseModel):
    """One source as the node's own file would spell it: an id, a kind and its options."""

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    id: str = Field(pattern=NAME)
    kind: str

    @field_validator("kind")
    @classmethod
    def _driven(cls, kind: str) -> str:
        if kind not in DRIVERS:
            later = PENDING.get(kind)
            reason = f"; that driver {later}" if later else ""
            raise ValueError(f"a satellite cannot be configured with {kind!r} sources{reason}")
        return kind

    def entry(self) -> dict:
        options = dict(self.model_extra or {})
        for name, value in options.items():
            if not isinstance(value, Scalar):
                raise ValueError(f"{self.id}.{name} must be a plain value")
            if isinstance(value, str) and len(value) > 128:
                raise ValueError(f"{self.id}.{name} is too long")
        return {"id": self.id, "kind": self.kind, **options}


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    node_id: str = Field(pattern=NAME)
    sources: list[SourceRequest] = Field(max_length=MAX_SOURCES)

    def entries(self) -> list[dict]:
        entries = [source.entry() for source in self.sources]
        names = [entry["id"] for entry in entries]
        if len(set(names)) != len(names):
            raise ValueError("each source on a node needs its own id")
        return entries


def overview(status: dict) -> dict:
    """The page's document, built from `SatelliteService.status()` plus the service error."""
    return {
        "enabled": bool(status.get("enabled")),
        "error": status.get("error"),
        "broker": status.get("broker"),
        "drivers": list(DRIVERS),
        "pending": dict(PENDING),
        "nodes": [_node(node) for node in status.get("nodes", [])],
    }


def _node(node: dict) -> dict:
    health = node.get("health") or {}
    reported = node.get("reported") or {}
    session = node.get("session") or {}
    measured = health.get("sources") or {}
    declared = {entry.get("source_id"): entry for entry in reported.get("sources", [])}
    sources = []
    for source in node.get("sources", []):
        name = source["source_id"].partition(".")[2] or source["source_id"]
        said = declared.get(name, {})
        reading = measured.get(name) or {}
        sources.append(
            {
                "source_id": source["source_id"],
                "name": name,
                "kind": said.get("kind"),
                "role": source["kind"],
                "state": source["state"],
                "declared": name in declared,
                "enabled": said.get("enabled", name in declared),
                "supported": said.get("kind") in DRIVERS,
                "options": said.get("options", {}),
                "last": reading.get("last"),
                "last_reading_age_seconds": reading.get("last_reading_age_seconds"),
                "driver": reading.get("driver"),
                "error": reading.get("error"),
            }
        )
    errors = [f"{source['name']}: {source['error']}" for source in sources if source.get("error")]
    refused = (health.get("events") or {}).get("reasons") or {}
    errors += [f"{count} × {reason}" for reason, count in sorted(refused.items())]
    if session.get("closed") and session.get("close_reason"):
        errors.append(f"session closed: {session['close_reason']}")
    return {
        "node_id": node["node_id"],
        "display_name": node.get("display_name") or node["node_id"],
        "status": node.get("status"),
        "zone": node.get("zone"),
        "profile": reported.get("profile") or node.get("profile"),
        "freshness": node.get("freshness"),
        "online": bool(session) and not session.get("closed"),
        "agent_version": reported.get("agent_version"),
        "config_revision": reported.get("config_revision"),
        "last_seen": health.get("last_seen"),
        "clock_status": health.get("clock_status"),
        "board": health.get("board") or {},
        "queue": health.get("queue") or {},
        "sources": sources,
        "errors": errors,
        "configuration": node.get("configuration"),
    }


__all__ = ["DRIVERS", "ConfigureRequest", "SourceRequest", "overview"]

"""The TOML file a satellite is installed with, read into something that refuses nonsense.

There is no Pydantic here and there will not be: the agent has to install on a board where
every dependency is a cost. The rules are written out instead, and the file is checked in
full before the agent does anything, so a typo is a refusal at `validate` rather than a
sensor that silently never reports.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentry_satellite.names import InvalidName, check_name, local_name

PROFILES = ("camera-sensor", "sensor-presence", "audio-sensor")
"""What a board is for. The profile decides which drivers are allowed to exist at all."""

SOURCE_KINDS = ("gpio", "onewire", "bme280", "adc", "csi", "uvc", "microphone", "ble", "dummy")

KINDS_BY_PROFILE = {
    "camera-sensor": {"gpio", "onewire", "bme280", "adc", "csi", "uvc", "microphone", "dummy"},
    "sensor-presence": {"gpio", "onewire", "bme280", "adc", "ble", "dummy"},
    "audio-sensor": {"gpio", "onewire", "bme280", "adc", "microphone", "dummy"},
}


class ConfigError(ValueError):
    """The configuration file cannot be used as written."""


@dataclass(frozen=True)
class Hub:
    mqtt_host: str
    mqtt_port: int = 8883
    protocol: int = 5


@dataclass(frozen=True)
class Tls:
    ca_file: Path
    cert_file: Path
    key_file: Path


@dataclass(frozen=True)
class Limits:
    event_max_count: int = 256
    event_max_bytes: int = 1048576
    event_max_age_seconds: int = 300
    heartbeat_seconds: int = 15


@dataclass(frozen=True)
class Source:
    """One thing to read, named by the name it has on this node."""

    id: str
    kind: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    node_id: str
    profile: str
    hub: Hub
    tls: Tls
    limits: Limits
    sources: tuple[Source, ...]
    identity_file: Path

    def source(self, source_id: str) -> Source:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)


def _section(document: dict, name: str) -> dict:
    value = document.get(name)
    if value is None:
        raise ConfigError(f"the [{name}] section is missing")
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _required(section: dict, key: str, kind: type, where: str):
    if key not in section:
        raise ConfigError(f"{where} is missing {key}")
    value = section[key]
    if isinstance(value, bool) is not (kind is bool) or not isinstance(value, kind):
        raise ConfigError(f"{where}.{key} must be {kind.__name__}")
    return value


def _positive(section: dict, key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where}.{key} must be a positive whole number")
    return value


def _unknown(section: dict, known: set[str], where: str) -> None:
    extra = sorted(set(section) - known)
    if extra:
        raise ConfigError(f"{where} does not take {', '.join(extra)}")


def parse(document: dict, *, identity_file: Path) -> Config:
    """Turn a parsed TOML document into a configuration, or say why it cannot be one."""
    node = _section(document, "node")
    _unknown(node, {"id", "profile"}, "[node]")
    try:
        node_id = check_name(_required(node, "id", str, "[node]"), "a node id")
    except InvalidName as error:
        raise ConfigError(f"[node]: {error}") from error
    profile = _required(node, "profile", str, "[node]")
    if profile not in PROFILES:
        raise ConfigError(f"[node].profile must be one of {', '.join(PROFILES)}")

    hub_section = _section(document, "hub")
    _unknown(hub_section, {"mqtt_host", "mqtt_port", "protocol"}, "[hub]")
    protocol = hub_section.get("protocol", 5)
    if protocol != 5:
        raise ConfigError("[hub].protocol must be 5: the hub speaks MQTT 5 and nothing else")
    hub = Hub(
        mqtt_host=_required(hub_section, "mqtt_host", str, "[hub]"),
        mqtt_port=_positive(hub_section, "mqtt_port", 8883, "[hub]"),
        protocol=5,
    )

    tls_section = _section(document, "tls")
    _unknown(tls_section, {"ca_file", "cert_file", "key_file"}, "[tls]")
    tls = Tls(
        ca_file=Path(_required(tls_section, "ca_file", str, "[tls]")),
        cert_file=Path(_required(tls_section, "cert_file", str, "[tls]")),
        key_file=Path(_required(tls_section, "key_file", str, "[tls]")),
    )

    limits_section = document.get("limits", {})
    if not isinstance(limits_section, dict):
        raise ConfigError("[limits] must be a table")
    _unknown(
        limits_section,
        {"event_max_count", "event_max_bytes", "event_max_age_seconds", "heartbeat_seconds"},
        "[limits]",
    )
    limits = Limits(
        event_max_count=_positive(limits_section, "event_max_count", 256, "[limits]"),
        event_max_bytes=_positive(limits_section, "event_max_bytes", 1048576, "[limits]"),
        event_max_age_seconds=_positive(limits_section, "event_max_age_seconds", 300, "[limits]"),
        heartbeat_seconds=_positive(limits_section, "heartbeat_seconds", 15, "[limits]"),
    )

    entries = document.get("sources", [])
    if not isinstance(entries, list):
        raise ConfigError("[[sources]] must be a list of tables")
    allowed = KINDS_BY_PROFILE[profile]
    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"[[sources]] {index + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        try:
            source_id = local_name(_required(entry, "id", str, where), node_id)
        except InvalidName as error:
            raise ConfigError(f"{where}: {error}") from error
        if source_id in seen:
            raise ConfigError(f"{where}: {source_id} is declared twice")
        seen.add(source_id)
        kind = _required(entry, "kind", str, where)
        if kind not in SOURCE_KINDS:
            raise ConfigError(f"{where}.kind must be one of {', '.join(SOURCE_KINDS)}")
        if kind not in allowed:
            raise ConfigError(
                f"{where}: a {profile} node has no {kind} sources."
                " A capability has to be in the profile before it can be configured."
            )
        options = {key: value for key, value in entry.items() if key not in {"id", "kind"}}
        sources.append(Source(id=source_id, kind=kind, options=options))

    _unknown(document, {"node", "hub", "tls", "limits", "sources"}, "the configuration")
    return Config(
        node_id=node_id,
        profile=profile,
        hub=hub,
        tls=tls,
        limits=limits,
        sources=tuple(sources),
        identity_file=identity_file,
    )


def load(path: Path, *, identity_file: Path | None = None) -> Config:
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ConfigError(f"{path} does not exist") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error
    return parse(document, identity_file=identity_file or path.parent / "identity.json")


def summary(config: Config) -> dict[str, Any]:
    """What is safe to print or log: names and numbers, never key material."""
    return {
        "node_id": config.node_id,
        "profile": config.profile,
        "hub": f"{config.hub.mqtt_host}:{config.hub.mqtt_port}",
        "sources": [f"{source.id} ({source.kind})" for source in config.sources],
        "limits": {
            "event_max_count": config.limits.event_max_count,
            "event_max_bytes": config.limits.event_max_bytes,
            "event_max_age_seconds": config.limits.event_max_age_seconds,
            "heartbeat_seconds": config.limits.heartbeat_seconds,
        },
    }

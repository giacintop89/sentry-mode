"""The TOML file a satellite is installed with, read into something that refuses nonsense.

There is no Pydantic here and there will not be: the agent has to install on a board where
every dependency is a cost. The rules are written out instead, and the file is checked in
full before the agent does anything, so a typo is a refusal at `validate` rather than a
sensor that silently never reports.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentry_satellite.names import InvalidName, check_kind, check_name, local_name

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


# -- what each kind of source takes ------------------------------------------------------
#
# Each option is (type, default, check). A default of REQUIRED means the file has to say
# it. The check returns an error message, or None when the value is acceptable. Options
# of kinds without a driver yet are kept as written, and `validate` says the driver is
# missing.

REQUIRED = object()
ONEWIRE_ID = r"^28-[0-9a-f]{12}$"
I2C_ADDRESSES = {"bme280": (0x76, 0x77), "adc": (0x48, 0x49, 0x4A, 0x4B)}
I2C_LINES = {1: (2, 3)}
I2S_LINES = (18, 19, 20, 21)
CSI_SIZES = ((320, 240), (640, 480), (1280, 720))
ALSA_DEVICE = r"^[A-Za-z0-9_][A-Za-z0-9_:=,.-]{0,63}$"


def _between(low: float, high: float) -> Any:
    return lambda value: None if low <= value <= high else f"must be from {low} to {high}"


def _one_of(*allowed: Any) -> Any:
    return lambda value: (
        None if value in allowed else f"must be one of {', '.join(map(str, allowed))}"
    )


def _kind(value: str) -> str | None:
    try:
        check_kind(value)
    except InvalidName as error:
        return str(error)
    return None


def _alsa_device(value: str) -> str | None:
    if re.fullmatch(ALSA_DEVICE, value):
        return None
    return "must be an ALSA device name, such as default or plughw:CARD=sndrpii2scard"


def _onewire_id(value: str) -> str | None:
    return None if re.fullmatch(ONEWIRE_ID, value) else "must look like 28-0123456789ab"


COMMON = {"enabled": (bool, True, None)}
INTERVAL = {"interval_seconds": (float, 30.0, _between(1, 3600))}
OPTIONS: dict[str, dict[str, tuple]] = {
    "gpio": {
        "chip": (str, "/dev/gpiochip0", None),
        "line_numbering": (str, REQUIRED, _one_of("bcm")),
        "line": (int, REQUIRED, _between(0, 53)),
        "active_high": (bool, True, None),
        "bias": (str, "disabled", _one_of("disabled", "pull_up", "pull_down", "as_is")),
        "debounce_ms": (int, 50, _between(0, 5000)),
        "settle_seconds": (float, 0.0, _between(0, 600)),
        "event_kind": (str, "sensor.motion", _kind),
    },
    "onewire": {
        "device": (str, REQUIRED, _onewire_id),
        "line": (int, 4, _between(0, 53)),
        "event_kind": (str, "climate.temperature", _kind),
        **INTERVAL,
    },
    "bme280": {
        "bus": (int, 1, _between(0, 20)),
        "address": (int, 0x76, _one_of(*I2C_ADDRESSES["bme280"])),
        "measure": (str, REQUIRED, _one_of("temperature", "humidity", "pressure")),
        **INTERVAL,
    },
    "adc": {
        "chip": (str, "ads1115", _one_of("ads1115")),
        "bus": (int, 1, _between(0, 20)),
        "address": (int, 0x48, _one_of(*I2C_ADDRESSES["adc"])),
        "channel": (int, REQUIRED, _between(0, 3)),
        "output": (str, "ratio", _one_of("ratio", "volts")),
        "reference_volts": (float, 3.3, _between(0.1, 4.096)),
        "event_kind": (str, "light.level", _kind),
        **INTERVAL,
    },
    "dummy": {"interval_seconds": (float, 1.0, _between(0.01, 3600))},
    # The one profile qualified on the Zero W: hardware H.264, sent as it comes out of the
    # encoder inside TLS. See docs/adr/satellite-video-profile.md on the hub.
    "csi": {
        "profile": (str, "h264-tls", _one_of("h264-tls")),
        "width": (int, 640, _one_of(*sorted({w for w, _ in CSI_SIZES}))),
        "height": (int, 480, _one_of(*sorted({h for _, h in CSI_SIZES}))),
        "fps": (int, 10, _between(1, 15)),
        "bitrate_kbps": (int, 1000, _between(100, 4000)),
        "keyframe_seconds": (float, 2.0, _between(0.5, 10)),
    },
    # Raw 16 kHz mono from ALSA. An I2S microphone holds the I2S pins; a USB one does not.
    "microphone": {
        "device": (str, "default", _alsa_device),
        "interface": (str, "i2s", _one_of("i2s", "usb")),
        "activity": (bool, False, None),
        "activity_threshold_dbfs": (float, -35.0, _between(-90, -1)),
        "activity_min_seconds": (float, 0.3, _between(0.1, 10)),
        "activity_hold_seconds": (float, 3.0, _between(0.5, 120)),
        "event_kind": (str, "audio.activity", _kind),
    },
}


def check_options(kind: str, options: dict[str, Any], where: str) -> dict[str, Any]:
    """The options with defaults filled in, or a refusal naming the first bad one."""
    schema = OPTIONS.get(kind)
    if schema is None:
        checked = dict(options)
        enabled = checked.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}.enabled must be bool")
        checked["enabled"] = enabled
        return checked
    schema = {**COMMON, **schema}
    _unknown(options, set(schema), where)
    checked = {}
    for name, (kind_of, default, check) in schema.items():
        if name not in options:
            if default is REQUIRED:
                raise ConfigError(f"{where} is missing {name}")
            checked[name] = default
            continue
        value = options[name]
        if kind_of is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if isinstance(value, bool) is not (kind_of is bool) or not isinstance(value, kind_of):
            raise ConfigError(f"{where}.{name} must be {kind_of.__name__}")
        problem = check(value) if check else None
        if problem:
            raise ConfigError(f"{where}.{name} {problem}")
        checked[name] = value
    return checked


def check_conflicts(sources: "list[Source]") -> None:
    """Two sources may not claim the same pin, probe, channel or measurement."""
    lines: dict[tuple[str, int], str] = {}
    reserved: dict[int, str] = {}
    devices: dict[tuple[int, int], tuple[str, str]] = {}
    parts: dict[tuple, str] = {}
    for source in sources:
        options = source.options
        if not options.get("enabled", True):
            continue
        if source.kind == "csi":
            if (options["width"], options["height"]) not in CSI_SIZES:
                sizes = ", ".join(f"{w}x{h}" for w, h in CSI_SIZES)
                raise ConfigError(f"{source.id}: a CSI camera streams at {sizes}")
            if ("csi",) in parts:
                raise ConfigError(
                    f"{source.id} and {parts[('csi',)]} both want the board's one camera port"
                )
            parts[("csi",)] = source.id
            continue
        if source.kind in ("bme280", "adc"):
            key = (options["bus"], options["address"])
            held = devices.get(key)
            if held is not None and held[0] != source.kind:
                raise ConfigError(
                    f"{source.id} and {held[1]} both use i2c-{key[0]} address 0x{key[1]:02x}"
                )
            devices.setdefault(key, (source.kind, source.id))
            part = options["measure"] if source.kind == "bme280" else options["channel"]
            if (key, part) in parts:
                raise ConfigError(
                    f"{source.id} and {parts[(key, part)]} read the same {part} "
                    f"on i2c-{key[0]} 0x{key[1]:02x}"
                )
            parts[(key, part)] = source.id
            for line in I2C_LINES.get(options["bus"], ()):
                reserved.setdefault(line, f"i2c-{options['bus']} (used by {source.id})")
        elif source.kind == "onewire":
            if ("onewire", options["device"]) in parts:
                raise ConfigError(
                    f"{source.id} and {parts[('onewire', options['device'])]} "
                    f"are the same probe {options['device']}"
                )
            parts[("onewire", options["device"])] = source.id
            reserved.setdefault(options["line"], f"the 1-Wire bus (used by {source.id})")
        elif source.kind == "microphone":
            if ("microphone", options["device"]) in parts:
                raise ConfigError(
                    f"{source.id} and {parts[('microphone', options['device'])]} "
                    f"both capture from {options['device']}"
                )
            parts[("microphone", options["device"])] = source.id
            if options["interface"] == "i2s":
                for line in I2S_LINES:
                    reserved.setdefault(line, f"the I2S microphone {source.id}")
    for source in sources:
        options = source.options
        if source.kind != "gpio" or not options.get("enabled", True):
            continue
        key = (options["chip"], options["line"])
        if key in lines:
            raise ConfigError(f"{source.id} and {lines[key]} both use BCM line {key[1]}")
        lines[key] = source.id
        if options["line"] in reserved:
            raise ConfigError(
                f"{source.id} uses BCM line {options['line']}, which belongs to "
                f"{reserved[options['line']]}"
            )


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

    @property
    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))


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


def parse_sources(
    entries: object,
    *,
    node_id: str,
    profile: str,
    kinds: "tuple[str, ...] | None" = None,
    where: str = "[[sources]]",
) -> tuple[Source, ...]:
    """The `[[sources]]` list, checked in full, conflicts included.

    `kinds` narrows what is accepted further than the profile does. Remote configuration
    uses it to refuse anything this agent has no driver for.
    """
    if not isinstance(entries, list):
        raise ConfigError(f"{where} must be a list of tables")
    allowed = KINDS_BY_PROFILE[profile]
    sources: list[Source] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        at = f"{where} {index + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{at} must be a table")
        try:
            source_id = local_name(_required(entry, "id", str, at), node_id)
        except InvalidName as error:
            raise ConfigError(f"{at}: {error}") from error
        if source_id in seen:
            raise ConfigError(f"{at}: {source_id} is declared twice")
        seen.add(source_id)
        kind = _required(entry, "kind", str, at)
        if kind not in SOURCE_KINDS:
            raise ConfigError(f"{at}.kind must be one of {', '.join(SOURCE_KINDS)}")
        if kind not in allowed:
            raise ConfigError(
                f"{at}: a {profile} node has no {kind} sources."
                " A capability has to be in the profile before it can be configured."
            )
        if kinds is not None and kind not in kinds:
            raise ConfigError(f"{at}: this agent has no {kind} driver installed")
        options = {key: value for key, value in entry.items() if key not in {"id", "kind"}}
        options = check_options(kind, options, f"{at} ({source_id})")
        sources.append(Source(id=source_id, kind=kind, options=options))
    check_conflicts(sources)
    return tuple(sources)


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

    sources = parse_sources(document.get("sources", []), node_id=node_id, profile=profile)

    _unknown(document, {"node", "hub", "tls", "limits", "sources"}, "the configuration")
    return Config(
        node_id=node_id,
        profile=profile,
        hub=hub,
        tls=tls,
        limits=limits,
        sources=sources,
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
        "sources": [
            f"{source.id} ({source.kind}" + ("" if source.enabled else ", disabled") + ")"
            for source in config.sources
        ],
        "limits": {
            "event_max_count": config.limits.event_max_count,
            "event_max_bytes": config.limits.event_max_bytes,
            "event_max_age_seconds": config.limits.event_max_age_seconds,
            "heartbeat_seconds": config.limits.heartbeat_seconds,
        },
    }

"""What kind of machine a satellite is, and what may therefore be asked of it.

A satellite used to be one thing: a Raspberry Pi Zero W running the Python agent, with a
filesystem, a camera port, ALSA and as much memory as a small program needs. The hub could
send it any driver it had, any number of sources, and options that named devices and
paths, because there was only one sort of node and it could take all of them.

A microcontroller cannot. It has the drivers that were compiled into its firmware and no
others, a few kilobytes for a configuration, no filesystem for a path to point into, and
no ALSA at all. A hub that sent it a camera source would not be making a mistake the node
could correct; it would be sending a message the node can only answer `failed`, after a
round trip, for a reason the page would have to guess at.

So the kinds of machine are written down here, administratively, and a configuration is
checked against the one a node is registered as before anything is sent. What a node may
actually do stays the intersection of this catalogue, the approval in the registry and the
lease it holds at the time: a platform entry says what is possible, never what is allowed.

Nothing here is taken from the node's own word. There is no field on the wire in which a
node declares what it supports, and if there were, it would be a hint for the hub to offer
something rather than an authorisation to do it.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

MAX_SOURCES = 32
"""The most any platform may have. A node's own limit may be lower; none may be higher."""


class Platform(BaseModel):
    """One kind of satellite: what it runs on, what it can drive, and what it cannot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=40)
    architecture: str = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=160)
    drivers: tuple[str, ...]
    max_sources: int = Field(ge=1, le=MAX_SOURCES)
    max_config_bytes: int = Field(ge=256)
    streams: tuple[str, ...] = ()
    """Which media a node of this kind could be asked for. Empty means it has no camera
    and no microphone the hub can stream, whatever anyone configures."""
    manual_tests: bool = True
    """Whether a source on this node can be tested from the page on demand."""
    experimental: bool = False
    """Shown as experimental everywhere it appears, because nothing has qualified it."""
    board_fields: tuple[str, ...] = ()
    """What this kind of board can say about itself in a heartbeat. A field outside this
    list is not something it measures, and a page that showed a zero for it would be
    showing a number nobody took."""


LINUX = Platform(
    name="linux-agent",
    architecture="linux",
    summary="A Raspberry Pi Zero W or similar running the Python satellite agent.",
    drivers=("gpio", "onewire", "bme280", "adc", "csi", "microphone", "ble", "board", "dummy"),
    max_sources=MAX_SOURCES,
    max_config_bytes=65536,
    streams=("video", "audio"),
    board_fields=("uptime_seconds", "temperature_c", "load1", "memory_available_kb", "throttled"),
)

# The two microcontroller entries are deliberately the same shape and deliberately small.
# The limits are the firmware's own, not a guess: `kMaxPlanned` in `plan.h` is eight
# sources on either chip, and a configuration reaches the node inside one MQTT packet and
# is kept in one flash sector, which leaves about two kilobytes for the sources once the
# command around them is counted. A hub that allowed more would be sending something the
# node can only refuse, or apply and then fail to remember.
#
# A Pico 2 W has been run against this hub; a Pico W has not. Both stay experimental until
# a board has been through the acceptance matrix, which no board has.
PICO_W = Platform(
    name="pico-w-sensor",
    architecture="rp2040",
    summary="A Pico W running the Sentry firmware: sensors only, no camera, no microphone.",
    drivers=("gpio", "onewire", "adc", "ble", "board"),
    max_sources=8,
    max_config_bytes=2048,
    streams=(),
    manual_tests=False,
    experimental=True,
    board_fields=("uptime_seconds", "temperature_c", "memory_available_kb", "reset"),
)

PICO_2W = PICO_W.model_copy(
    update={
        "name": "pico-2w-sensor",
        "architecture": "rp2350",
        "summary": "A Pico 2 W running the Sentry firmware: sensors only, with more room.",
        # More room in memory and in flash, and the same firmware: the limits on what it
        # may be configured with come from the firmware, so they are the same as well.
    }
)

CATALOGUE: dict[str, Platform] = {platform.name: platform for platform in (LINUX, PICO_W, PICO_2W)}

DEFAULT = LINUX.name
"""What a node registered before this catalogue existed is: the agent, as it always was."""


def platform(name: str | None) -> Platform:
    """The platform by name, falling back to the agent for anything unregistered.

    Falling back rather than raising is deliberate: every node in every existing registry
    predates this file, and a hub that refused to show them would be a worse hub than one
    that shows them as what they have always been.
    """
    if name is None:
        return CATALOGUE[DEFAULT]
    return CATALOGUE.get(name, CATALOGUE[DEFAULT])


def known(name: str) -> bool:
    return name in CATALOGUE


# What each driver on a microcontroller takes: the options it has a place for, and the
# ones it cannot start without. The firmware is the authority — this is `kBoardOptions` and
# its neighbours in `firmware/pico/src/core/plan.cpp` — and it is written down again here
# for the same reason the rest of this file is: so that a mistake the hub can see is an
# error on the page rather than a round trip that can only come back `failed`.
#
# `enabled` is left out on purpose. It is a field of every source rather than an option of
# any driver, and both sides treat it that way.
FIRMWARE_OPTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "board": ((), ("measure", "interval_seconds")),
    "gpio": (
        ("pin",),
        ("pin", "active_high", "bias", "debounce_ms", "settle_seconds", "event_kind"),
    ),
    "adc": (("pin",), ("pin", "output", "event_kind", "interval_seconds")),
    "onewire": (("pin",), ("pin", "device", "event_kind", "interval_seconds")),
    # `ble` needs one of two things rather than any one thing, so its required half is
    # empty and the choice is made below, where it can be said in words.
    "ble": (
        (),
        (
            "address",
            "ibeacon_uuid",
            "ibeacon_major",
            "ibeacon_minor",
            "rssi_min",
            "enter_sightings",
            "enter_window_seconds",
            "absent_after_seconds",
            "event_kind",
        ),
    ),
}

# An option that names something only a Linux machine has. Sending one to a microcontroller
# is not a small mistake: `/dev/video0` on a board with no filesystem is a string that can
# only be stored, never acted on, and the node would have to answer `failed` for it.
LINUX_ONLY_OPTIONS = ("device", "path", "command", "alsa_device", "video_device", "process")


def _looks_like_a_linux_thing(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith("/") or value.startswith("hw:") or "/dev/" in value


def check_configuration(chosen: Platform, entries: list[dict]) -> list[str]:
    """Everything wrong with this configuration for this kind of node, or nothing.

    The node checks all of it again and has the last word. This exists so that a mistake
    the hub can see is an error on the page rather than an `applied` that never happens.
    """
    reasons: list[str] = []
    if len(entries) > chosen.max_sources:
        reasons.append(
            f"{len(entries)} sources: a {chosen.name} takes at most {chosen.max_sources}"
        )
    for entry in entries:
        name = entry.get("id", "a source")
        kind = entry.get("kind")
        if kind not in chosen.drivers:
            reasons.append(f"{name}: a {chosen.name} has no {kind} driver")
        if chosen.architecture == "linux":
            continue
        # What this driver has a place for. A kind the firmware does not have at all has
        # been refused by name already, and nothing more can be said about its options.
        needed, known = FIRMWARE_OPTIONS.get(kind, ((), ()))
        for option in needed:
            if entry.get(option) is None:
                reasons.append(f"{name}: a {kind} source needs {option}, and this one names none")
        if kind == "ble":
            # One device, named the one way or the other. Both is two claims about what is
            # being watched, and neither is a source that would match every phone that goes
            # past the window — which the firmware refuses and so does this.
            named = [
                option for option in ("address", "ibeacon_uuid") if entry.get(option) is not None
            ]
            if len(named) != 1:
                reasons.append(
                    f"{name}: a device is watched by its address or by its iBeacon, not "
                    + ("both" if named else "neither")
                )
            if "address" in named and (
                entry.get("ibeacon_major") is not None or entry.get("ibeacon_minor") is not None
            ):
                reasons.append(
                    f"{name}: ibeacon_major and ibeacon_minor are parts of an iBeacon, and "
                    "this one watches an address"
                )
        for option, value in entry.items():
            if option in ("id", "kind", "enabled") or option in known:
                continue
            # An option this driver knows is this driver's business even when the agent
            # uses the same word for something else: `device` on a 1-Wire source is a probe
            # on a bus, not a sound card.
            if option in LINUX_ONLY_OPTIONS or _looks_like_a_linux_thing(value):
                reasons.append(
                    f"{name}.{option}: a {chosen.name} has no filesystem and no audio stack"
                )
            elif kind in FIRMWARE_OPTIONS:
                reasons.append(
                    f"{name}.{option}: a {kind} source on a {chosen.name} has no {option}"
                )
    size = _size_of(entries)
    if size > chosen.max_config_bytes:
        reasons.append(
            f"the configuration is {size} bytes; a {chosen.name} holds {chosen.max_config_bytes}"
        )
    return reasons


def _size_of(entries: list[dict]) -> int:
    """How large this configuration is on the wire, which is what the node has to hold."""
    return len(json.dumps(entries, separators=(",", ":")).encode())


def visible_board(chosen: Platform, board: dict) -> dict:
    """The board numbers this kind of node can actually produce.

    A microcontroller has no load average and no throttling register. The agent's health
    model leaves them null, and a page that showed `0` for them would be inventing a
    measurement; showing them at all invites somebody to read the null as a fault.
    """
    if not board:
        return {}
    return {key: value for key, value in board.items() if key in chosen.board_fields}


def as_document(chosen: Platform) -> dict:
    """The catalogue entry as the page reads it."""
    return {
        "name": chosen.name,
        "architecture": chosen.architecture,
        "summary": chosen.summary,
        "drivers": list(chosen.drivers),
        "max_sources": chosen.max_sources,
        "max_config_bytes": chosen.max_config_bytes,
        "streams": list(chosen.streams),
        "manual_tests": chosen.manual_tests,
        "experimental": chosen.experimental,
        "board_fields": list(chosen.board_fields),
    }


__all__ = [
    "CATALOGUE",
    "FIRMWARE_OPTIONS",
    "DEFAULT",
    "Platform",
    "as_document",
    "check_configuration",
    "known",
    "platform",
    "visible_board",
]

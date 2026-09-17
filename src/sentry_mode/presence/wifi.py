"""Wi-Fi presence: the shape of a router that could answer, and what its answer means.

A satellite says what its own radio saw. A router knows something no satellite can see:
which devices are associated with the access point right now. This module is where that
answer would arrive. It defines what a provider has to give and what the hub is allowed
to conclude from it, and deliberately stops there: no router is supported, because
supporting one means a read-only service identity on a named firmware, a response shape
that is pinned rather than scraped, a timeout and a rate limit, all of it proven against
the real box. Scanning access points from a satellite is a different thing entirely and
does not answer this question.

Two mistakes are easy to make here and both are refused. A DHCP lease is a promise about
an address, not a device on the radio: a phone that left an hour ago keeps its lease, so
a lease alone never makes anyone present. And an answer that has been sitting around is
not an answer: past `fresh_seconds` the hub says `unknown` rather than repeating what was
true a while ago. Nothing about a device is ever concluded from silence.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"
"""The same three words a satellite uses, so a rule reads the same either way."""

VALID, UNAVAILABLE = "valid", "unavailable"

MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")

FRESH_SECONDS = 60.0
"""How long a router's answer is worth repeating. Longer than one association poll on any
consumer box, short enough that a device that left is not still present a minute later."""


class ProviderError(RuntimeError):
    """The router could not be asked, or did not answer the way it promised."""


def normalize(mac: str) -> str:
    """The one spelling of an address this module compares: upper case, colons."""
    cleaned = mac.strip().upper().replace("-", ":")
    if not MAC.match(cleaned):
        raise ValueError(f"{mac!r} is not a MAC address")
    return cleaned


@dataclass(frozen=True)
class Client:
    """One device as a router describes it.

    `associated` is the only field that can make a device present. `leased_until` is kept
    because it explains an answer in a log line, never because it changes one.
    """

    mac: str
    associated: bool
    hostname: str | None = None
    leased_until: float | None = None


@dataclass(frozen=True)
class Snapshot:
    """What a provider saw, and when the router itself saw it, in epoch seconds."""

    observed_at: float
    clients: tuple[Client, ...]

    def find(self, mac: str) -> Client | None:
        return next((one for one in self.clients if one.mac == mac), None)


class PresenceProvider(Protocol):
    """A read-only view of one router's client table.

    `look` either returns what the router said or raises `ProviderError`. It may not
    block for long and may not guess: an implementation that cannot tell an association
    from a lease belongs in neither half of this protocol.
    """

    name: str

    def look(self) -> Snapshot: ...


@dataclass(frozen=True)
class Finding:
    """What the hub would say about one device, and why it says it."""

    source_id: str
    value: str | None
    quality: str
    reason: str


class SnapshotProvider:
    """A provider that reads a file someone else writes.

    This is not a router adapter. It exists so the rules can be exercised, and so an
    operator who does have a way to dump their router's client table can try the idea out
    by writing that dump to a path, without the hub pretending it knows any firmware.
    """

    def __init__(self, path: Path | str, *, name: str = "snapshot") -> None:
        self.path = Path(path)
        self.name = name

    def look(self) -> Snapshot:
        try:
            raw = json.loads(self.path.read_text())
        except OSError as error:
            raise ProviderError(f"{self.path} cannot be read: {error}") from error
        except json.JSONDecodeError as error:
            raise ProviderError(f"{self.path} is not the JSON it should be: {error}") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("clients", []), list):
            raise ProviderError(f"{self.path} holds no client table")
        clients = []
        for entry in raw.get("clients", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("mac"), str):
                raise ProviderError(f"{self.path} holds a client without an address")
            try:
                mac = normalize(entry["mac"])
            except ValueError as error:
                raise ProviderError(f"{self.path}: {error}") from error
            clients.append(
                Client(
                    mac=mac,
                    associated=bool(entry.get("associated")),
                    hostname=entry.get("hostname"),
                    leased_until=entry.get("lease_expires_at"),
                )
            )
        observed = raw.get("observed_at")
        if not isinstance(observed, (int, float)):
            observed = self.path.stat().st_mtime  # an undated dump is as old as its file
        return Snapshot(observed_at=float(observed), clients=tuple(clients))


class WifiPresence:
    """One provider, the devices a hub cares about, and the three answers it may give.

    `devices` maps a source id to the address to look for. The addresses stay here and in
    the provider: a finding carries the source id and never the MAC, so the rest of the
    hub, its log and its UI talk about `hall.phone` rather than about a radio identifier.
    """

    def __init__(
        self,
        provider: PresenceProvider,
        devices: Mapping[str, str],
        *,
        fresh_seconds: float = FRESH_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.provider = provider
        self.devices = {source_id: normalize(mac) for source_id, mac in devices.items()}
        self.fresh_seconds = fresh_seconds
        self.clock = clock

    def read(self) -> list[Finding]:
        try:
            snapshot = self.provider.look()
        except ProviderError as error:
            return self._all(f"{self.provider.name} could not be asked: {error}")
        age = self.clock() - snapshot.observed_at
        if age > self.fresh_seconds:
            return self._all(f"{self.provider.name} last answered {age:.0f} s ago")
        if age < -self.fresh_seconds:
            return self._all(f"{self.provider.name} answered with a time in the future")
        return [self._one(source_id, snapshot) for source_id in self.devices]

    def _all(self, reason: str) -> list[Finding]:
        return [Finding(source_id, None, UNAVAILABLE, reason) for source_id in self.devices]

    def _one(self, source_id: str, snapshot: Snapshot) -> Finding:
        client = snapshot.find(self.devices[source_id])
        if client is None:
            return Finding(source_id, ABSENT, VALID, f"{self.provider.name} knows no such device")
        if client.associated:
            return Finding(source_id, PRESENT, VALID, f"associated with {self.provider.name}")
        if client.leased_until is not None:
            return Finding(source_id, ABSENT, VALID, "holds a lease but is not associated")
        return Finding(source_id, ABSENT, VALID, f"not associated with {self.provider.name}")

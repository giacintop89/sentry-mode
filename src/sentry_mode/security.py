"""What the network can reach, checked against what it is supposed to reach.

The satellites live on a network of their own, and only two things on this hub belong on
it: the broker they publish to and the media gateway they send pictures and sound to.
The dashboard is administrative — it arms and disarms, it edits rules, it opens the
microphone — and a satellite that has been taken over should not be able to knock on its
door at all. A wildcard bind is the usual way that happens by accident, so it is checked
for rather than assumed away.

This says what is reachable, and says it in the log at startup. It does not close a
socket on anyone: the person who set the interfaces up decides what to do about it, and a
hub that refused to start because of a subnet would be worse than one that says so.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass

log = logging.getLogger(__name__)

WILDCARD = ("0.0.0.0", "::", "")
"""Binds that mean every interface this machine has, including the satellite one."""

FOR_SATELLITES = ("media gateway", "broker")
"""The listeners that are supposed to be reachable from the satellite network."""


@dataclass(frozen=True)
class Finding:
    """One listener a satellite could reach that it has no business reaching."""

    listener: str
    host: str
    port: int
    reason: str


def addresses() -> list[str]:
    """Every address this machine answers on, as far as it can tell."""
    found = set()
    for family, _, _, _, address in socket.getaddrinfo(socket.gethostname(), None):
        if family in (socket.AF_INET, socket.AF_INET6) and isinstance(address[0], str):
            found.add(address[0].split("%")[0])
    return sorted(found)


def exposure(
    *,
    listeners: list[tuple[str, str, int]],
    satellite_network: str | None,
    addresses: list[str],
) -> list[Finding]:
    """The administrative listeners a satellite on `satellite_network` could reach.

    A listener bound to one address is judged by that address; one bound to a wildcard is
    judged by every address this machine has, because that is what it answers on.
    """
    if not satellite_network:
        return []
    try:
        network = ipaddress.ip_network(satellite_network, strict=False)
    except ValueError:
        log.warning("%r is not a network, so nothing was checked against it", satellite_network)
        return []
    found = []
    for name, host, port in listeners:
        if name in FOR_SATELLITES:
            continue
        reachable = [
            one for one in (addresses if host in WILDCARD else [host]) if _inside(one, network)
        ]
        if not reachable:
            continue
        where = "every interface" if host in WILDCARD else host
        found.append(
            Finding(
                listener=name,
                host=host,
                port=port,
                reason=(
                    f"{name} listens on {where} and is reachable from the satellite network "
                    f"{network} at {', '.join(reachable)}"
                ),
            )
        )
    return found


def _inside(address: str, network) -> bool:
    try:
        return ipaddress.ip_address(address) in network
    except ValueError:
        return False


def report(findings: list[Finding]) -> None:
    """Say it once, at startup, in the log the operator already reads."""
    for finding in findings:
        log.warning("%s. Bind it to the house network instead.", finding.reason)

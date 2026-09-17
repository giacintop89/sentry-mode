"""Connections, epochs and grants: who is talking to us right now, and what they may say.

A registered node is allowed to exist. A session is what makes it allowed to speak, and
only for a while. Every session gets a number that has never been used before, so that
anything arriving from an older one — a delayed message, a stale retained snapshot, a Last
Will that took the long way round — can be recognised as old rather than believed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Callable

from sentry_mode.satellites.config import HealthConfig, SessionsConfig

CAPABILITIES = ("events", "video", "audio")


class BrokerState(str, Enum):
    """What the link itself is doing, which is not the same as what the nodes are doing."""

    STOPPED = "stopped"
    CONNECTED = "connected"
    UNAVAILABLE = "transport_unavailable"


class Freshness(str, Enum):
    """How recently a node proved it was there."""

    LIVE = "live"
    STALE = "stale"
    OFFLINE = "offline"
    UNAVAILABLE = "transport_unavailable"
    NEVER_SEEN = "never_seen"


class SessionError(RuntimeError):
    """The session named does not exist, or is not the current one."""


@dataclass(frozen=True)
class Grant:
    """Permission for one node to use one capability, until it expires."""

    grant_id: str
    node_id: str
    capability: str
    hub_epoch: int
    issued_at: float
    expires_at: float
    sequence: int = 0

    def live(self, now: float) -> bool:
        return now < self.expires_at

    def as_command(self, *, action: str = "grant") -> dict:
        return {
            "command_id": str(uuid.uuid4()),
            "action": action,
            "node_id": self.node_id,
            "hub_epoch": self.hub_epoch,
            "capability": self.capability,
            "grant_id": self.grant_id,
            "duration_seconds": round(self.expires_at - self.issued_at, 3),
            "sequence": self.sequence,
        }


@dataclass
class Session:
    """One connection from one node, and everything that is true only while it lasts."""

    node_id: str
    connection_id: str
    boot_id: str | None
    hub_epoch: int
    opened_at: float
    last_seen: float
    agent_version: str | None = None
    closed: bool = False
    close_reason: str | None = None
    grants: dict[str, Grant] = field(default_factory=dict)

    def grant_for(self, capability: str, now: float) -> Grant | None:
        grant = self.grants.get(capability)
        return grant if grant is not None and grant.live(now) else None


@dataclass
class Comings:
    """How often a node has come back, and how often it came back as a different boot.

    A node that reconnects has a new connection; a node that restarted has a new boot id
    as well. Keeping the two apart is the difference between a flaky link and a board that
    is resetting, and from the outside they look the same: the node is there again.
    """

    connections: int = 0
    boots: int = 0
    last_boot_id: str | None = None
    restarted_at: float | None = None

    @property
    def restarts(self) -> int:
        """Boots after the first one. The first is how the hub met it, not a restart."""
        return max(self.boots - 1, 0)


class SessionManager:
    """Hands out epochs and grants, and knows when to stop believing a node is there."""

    def __init__(
        self,
        *,
        health: HealthConfig,
        sessions: SessionsConfig,
        next_epoch: Callable[[], int],
        clock: Callable[[], float] = time.monotonic,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._health = health
        self._settings = sessions
        self._next_epoch = next_epoch
        self._clock = clock
        self._new_id = new_id
        self._lock = RLock()
        # Kept across sessions on purpose: a node that has reset six times has reset six
        # times whether or not it is connected at the moment somebody looks.
        self._comings: dict[str, Comings] = {}
        self._sessions: dict[str, Session] = {}
        self._broker = BrokerState.STOPPED

    # -- the link ----------------------------------------------------------------

    @property
    def broker(self) -> BrokerState:
        with self._lock:
            return self._broker

    def broker_connected(self) -> None:
        with self._lock:
            self._broker = BrokerState.CONNECTED

    def broker_unavailable(self) -> None:
        """The broker is gone, which says nothing about any individual node.

        Every satellite going quiet at the same instant is one fault, not fifteen. The
        sessions are kept so that a node that is still there is recognised when the link
        comes back, and nothing is reported as offline in the meantime.
        """
        with self._lock:
            self._broker = BrokerState.UNAVAILABLE

    def broker_stopped(self) -> None:
        with self._lock:
            self._broker = BrokerState.STOPPED
            self._sessions.clear()

    # -- sessions ----------------------------------------------------------------

    def open(
        self,
        node_id: str,
        *,
        connection_id: str,
        boot_id: str | None = None,
        agent_version: str | None = None,
    ) -> Session:
        """Start a session, replacing any older one for the same node."""
        with self._lock:
            now = self._clock()
            session = Session(
                node_id=node_id,
                connection_id=connection_id,
                boot_id=boot_id,
                hub_epoch=self._next_epoch(),
                opened_at=now,
                last_seen=now,
                agent_version=agent_version,
            )
            self._sessions[node_id] = session
            comings = self._comings.setdefault(node_id, Comings())
            comings.connections += 1
            if boot_id is not None and boot_id != comings.last_boot_id:
                comings.boots += 1
                if comings.last_boot_id is not None:
                    comings.restarted_at = now
                comings.last_boot_id = boot_id
            return session

    def current(self, node_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(node_id)
            return None if session is None or session.closed else session

    def heartbeat(self, node_id: str, *, connection_id: str | None = None) -> Session:
        with self._lock:
            session = self._require(node_id, connection_id)
            session.last_seen = self._clock()
            return session

    def close(
        self, node_id: str, *, connection_id: str | None = None, reason: str = "closed"
    ) -> bool:
        """End a session, unless the news is about a connection that is already history.

        A Last Will can arrive after the node has already reconnected. Acting on it would
        mark a node that is present as gone, so a message about an older connection is
        counted and dropped.
        """
        with self._lock:
            session = self._sessions.get(node_id)
            if session is None:
                return False
            if connection_id is not None and session.connection_id != connection_id:
                return False
            session.closed = True
            session.close_reason = reason
            session.grants.clear()
            return True

    def is_current(self, node_id: str, *, connection_id: str, hub_epoch: int | None = None) -> bool:
        """Whether something that claims to come from this connection still counts."""
        session = self.current(node_id)
        if session is None or session.connection_id != connection_id:
            return False
        return hub_epoch is None or hub_epoch == session.hub_epoch

    # -- grants ------------------------------------------------------------------

    def grant(self, node_id: str, capability: str, *, seconds: float | None = None) -> Grant:
        if capability not in CAPABILITIES:
            raise ValueError(f"capability must be one of {', '.join(CAPABILITIES)}")
        with self._lock:
            session = self._require(node_id, None)
            now = self._clock()
            duration = float(seconds if seconds is not None else self._settings.grant_seconds)
            issued = Grant(
                grant_id=self._new_id(),
                node_id=node_id,
                capability=capability,
                hub_epoch=session.hub_epoch,
                issued_at=now,
                expires_at=now + duration,
            )
            session.grants[capability] = issued
            return issued

    def renew(self, node_id: str, capability: str, *, seconds: float | None = None) -> Grant:
        """Extend a grant, keeping its identity and counting the renewal.

        The sequence is what stops a retransmitted renewal from holding a session open for
        ever: the agent refuses one that is not newer than the grant it renews.
        """
        with self._lock:
            session = self._require(node_id, None)
            held = session.grants.get(capability)
            if held is None:
                raise SessionError(f"{node_id} holds no {capability} grant to renew")
            now = self._clock()
            duration = float(seconds if seconds is not None else self._settings.grant_seconds)
            renewed = Grant(
                grant_id=held.grant_id,
                node_id=node_id,
                capability=capability,
                hub_epoch=session.hub_epoch,
                issued_at=now,
                expires_at=now + duration,
                sequence=held.sequence + 1,
            )
            session.grants[capability] = renewed
            return renewed

    def revoke(self, node_id: str, capability: str | None = None) -> list[str]:
        """Take back one capability, or all of them. Used on revocation and on shutdown."""
        with self._lock:
            session = self._sessions.get(node_id)
            if session is None:
                return []
            taken = [capability] if capability else list(session.grants)
            for name in taken:
                session.grants.pop(name, None)
            return [name for name in taken]

    def grant_is_current(self, node_id: str, grant_id: str, *, capability: str = "events") -> bool:
        session = self.current(node_id)
        if session is None:
            return False
        grant = session.grant_for(capability, self._clock())
        return grant is not None and grant.grant_id == grant_id

    def granted(self, node_id: str, capability: str = "events") -> Grant | None:
        """The live grant a node holds now, by this manager's clock."""
        session = self.current(node_id)
        return None if session is None else session.grant_for(capability, self._clock())

    def due_for_renewal(self) -> list[Grant]:
        """The grants that should be renewed now, before anybody notices them lapsing."""
        with self._lock:
            now = self._clock()
            deadline = self._settings.renew_every_seconds
            return [
                grant
                for session in self._sessions.values()
                if not session.closed
                for grant in session.grants.values()
                if grant.expires_at - now <= deadline
            ]

    # -- what the rest of the hub asks -------------------------------------------

    def freshness(self, node_id: str) -> Freshness:
        with self._lock:
            if self._broker is not BrokerState.CONNECTED:
                return Freshness.UNAVAILABLE
            session = self._sessions.get(node_id)
            if session is None:
                return Freshness.NEVER_SEEN
            if session.closed:
                return Freshness.OFFLINE
            silence = self._clock() - session.last_seen
            if silence >= self._health.offline_after_seconds:
                return Freshness.OFFLINE
            if silence >= self._health.stale_after_seconds:
                return Freshness.STALE
            return Freshness.LIVE

    def status(self) -> dict:
        """One picture with the link and the nodes kept apart, because they fail apart."""
        with self._lock:
            now = self._clock()
            return {
                "broker": self._broker.value,
                "nodes": {
                    node_id: {
                        "connection_id": session.connection_id,
                        "boot_id": session.boot_id,
                        "hub_epoch": session.hub_epoch,
                        "freshness": self.freshness(node_id).value,
                        "silent_for_seconds": round(now - session.last_seen, 1),
                        "closed": session.closed,
                        "close_reason": session.close_reason,
                        "connections": self._comings[node_id].connections,
                        "restarts": self._comings[node_id].restarts,
                        "restarted_seconds_ago": (
                            None
                            if self._comings[node_id].restarted_at is None
                            else round(now - self._comings[node_id].restarted_at, 1)
                        ),
                        "grants": {
                            capability: {
                                "grant_id": grant.grant_id,
                                "expires_in_seconds": round(grant.expires_at - now, 1),
                                "sequence": grant.sequence,
                            }
                            for capability, grant in session.grants.items()
                        },
                    }
                    for node_id, session in self._sessions.items()
                },
            }

    def _require(self, node_id: str, connection_id: str | None) -> Session:
        session = self._sessions.get(node_id)
        if session is None or session.closed:
            raise SessionError(f"{node_id} has no open session")
        if connection_id is not None and session.connection_id != connection_id:
            raise SessionError(f"{node_id} is on a newer connection")
        return session

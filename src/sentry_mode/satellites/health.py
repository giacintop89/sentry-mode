"""What each node is reporting about itself, kept in memory and never in the journal.

A heartbeat every fifteen seconds is not history. Writing each one down would fill the
journal with the fact that nothing happened, so the latest reading replaces the one before
it and only the count of what arrived is kept. Transitions, refusals and faults are the
things that go in the journal; this is the current picture.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# A reading that waited longer than this is a node that could not send when it wanted to.
# The board publishes as soon as it has a link and a grant, so a quiet one reports single
# milliseconds; two seconds is well past a burst of baselines after a reconfiguration and
# well short of an outage anybody would notice another way.
BEHIND_MS = 2000

# How long the worst wait is worth remembering. After this it is history rather than a
# picture of now, and the next reading's own wait becomes the worst again.
WORST_FOR_SECONDS = 300.0


@dataclass
class NodeHealth:
    """The last thing a node said about itself, with the hub's own timestamps on it."""

    node_id: str
    heartbeats: int = 0
    last_seen: float | None = None
    agent_uptime_seconds: float | None = None
    clock_status: str | None = None
    queue: dict = field(default_factory=dict)
    board: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    accepted: int = 0
    refused: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    waited_ms: int | None = None
    worst_waited_ms: int | None = None
    worst_waited_at: float | None = None
    behind: bool = False

    def as_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "heartbeats": self.heartbeats,
            "last_seen": self.last_seen,
            "agent_uptime_seconds": self.agent_uptime_seconds,
            "clock_status": self.clock_status,
            "queue": dict(self.queue),
            "board": dict(self.board),
            "sources": {name: dict(value) for name, value in self.sources.items()},
            "events": {
                "accepted": self.accepted,
                "refused": self.refused,
                "reasons": dict(self.reasons),
            },
            # What the node said about how long its last reading waited, and the worst it
            # has said lately. The node measures this on its own monotonic clock, so it
            # survives the hub and the node disagreeing about what time it is.
            "waited": {
                "last_ms": self.waited_ms,
                "worst_ms": self.worst_waited_ms,
                "worst_at": self.worst_waited_at,
                "behind": self.behind,
                "behind_over_ms": BEHIND_MS,
            },
        }


class HealthBoard:
    """One entry per node, replaced rather than appended, plus how the journal is doing."""

    def __init__(self, *, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._nodes: dict[str, NodeHealth] = {}
        self._store: dict = {"available": False, "error": None}

    def _entry(self, node_id: str) -> NodeHealth:
        entry = self._nodes.get(node_id)
        if entry is None:
            entry = NodeHealth(node_id=node_id)
            self._nodes[node_id] = entry
        return entry

    def heartbeat(self, node_id: str, document: dict) -> None:
        """Take the newest reading. Nothing is added up and nothing is written to disk."""
        with self._lock:
            entry = self._entry(node_id)
            entry.heartbeats += 1
            entry.last_seen = self._clock()
            for name, value in (
                ("agent_uptime_seconds", document.get("agent_uptime_seconds")),
                ("clock_status", document.get("clock_status")),
            ):
                if value is not None:
                    setattr(entry, name, value)
            for name in ("queue", "board", "sources"):
                value = document.get(name)
                if isinstance(value, dict):
                    setattr(entry, name, value)

    def waited(self, node_id: str, waited_ms: int | None) -> None:
        """How long the node held the reading that has just arrived.

        Nothing else the hub sees says this. A node that has fallen behind is online, its
        heartbeats arrive, its sources are ready, and the only difference is that what it
        sends is old — which is exactly what this number is. It is logged when it crosses,
        once, and logged again when it comes back, because an operator needs the second
        line as much as the first.
        """
        if waited_ms is None:
            return
        with self._lock:
            entry = self._entry(node_id)
            now = self._clock()
            entry.waited_ms = waited_ms
            stale = entry.worst_waited_at is None or now - entry.worst_waited_at > WORST_FOR_SECONDS
            if stale or entry.worst_waited_ms is None or waited_ms >= entry.worst_waited_ms:
                entry.worst_waited_ms = waited_ms
                entry.worst_waited_at = now
            behind = waited_ms > BEHIND_MS
            if behind and not entry.behind:
                log.warning(
                    "%s is falling behind: its last reading waited %d ms before it could be sent",
                    node_id,
                    waited_ms,
                )
            elif entry.behind and not behind:
                log.info("%s has caught up: its last reading waited %d ms", node_id, waited_ms)
            entry.behind = behind

    def accepted(self, node_id: str) -> None:
        with self._lock:
            self._entry(node_id).accepted += 1

    def refused(self, node_id: str, reason: str) -> None:
        with self._lock:
            entry = self._entry(node_id)
            entry.refused += 1
            entry.reasons[reason] = entry.reasons.get(reason, 0) + 1

    def forget(self, node_id: str) -> None:
        """Drop a node that is no longer anybody's business, such as one just revoked."""
        with self._lock:
            self._nodes.pop(node_id, None)

    def journal(self, status: dict) -> None:
        with self._lock:
            self._store = dict(status)

    def of(self, node_id: str) -> dict | None:
        with self._lock:
            entry = self._nodes.get(node_id)
            return entry.as_dict() if entry else None

    def status(self) -> dict:
        with self._lock:
            return {
                "journal": dict(self._store),
                "nodes": {name: entry.as_dict() for name, entry in self._nodes.items()},
            }


__all__ = ["BEHIND_MS", "WORST_FOR_SECONDS", "HealthBoard", "NodeHealth"]

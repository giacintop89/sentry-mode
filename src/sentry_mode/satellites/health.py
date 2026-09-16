"""What each node is reporting about itself, kept in memory and never in the journal.

A heartbeat every fifteen seconds is not history. Writing each one down would fill the
journal with the fact that nothing happened, so the latest reading replaces the one before
it and only the count of what arrived is kept. Transitions, refusals and faults are the
things that go in the journal; this is the current picture.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


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


__all__ = ["HealthBoard", "NodeHealth"]

"""The one place that says which sources exist.

Local devices are declared by the node's own configuration and satellites announce
themselves later; both end up here, so a rule, an action or a page can ask a single
question instead of knowing which half of the system owns the answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from threading import RLock

from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState


class UnknownSource(LookupError):
    """A source was named that the node has never been told about."""


class SourceRegistry:
    """A thread-safe table of sources, keyed by an identifier that never changes."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, SourceRecord] = {}

    def register(self, record: SourceRecord) -> SourceRecord:
        """Add a source, or refuse if that identifier already means something else."""
        with self._lock:
            existing = self._records.get(record.id)
            if existing is not None:
                if existing.kind is not record.kind or existing.origin != record.origin:
                    raise ValueError(
                        f"source {record.id} is already registered as"
                        f" a {existing.origin} {existing.kind.value}"
                    )
                return existing
            self._records[record.id] = record
            return record

    def get(self, ref: SourceRef | str) -> SourceRecord | None:
        key = ref.id if isinstance(ref, SourceRef) else ref
        with self._lock:
            return self._records.get(key)

    def require(self, ref: SourceRef | str) -> SourceRecord:
        record = self.get(ref)
        if record is None:
            key = ref.id if isinstance(ref, SourceRef) else ref
            raise UnknownSource(key)
        return record

    def set_state(self, ref: SourceRef | str, state: SourceState) -> SourceRecord:
        """Record how a source is doing, leaving its identity untouched."""
        with self._lock:
            record = self.require(ref)
            updated = record.model_copy(update={"state": state})
            self._records[record.id] = updated
            return updated

    def of_kind(self, kind: SourceKind) -> list[SourceRecord]:
        with self._lock:
            return [record for record in self._records.values() if record.kind is kind]

    def all(self) -> list[SourceRecord]:
        with self._lock:
            return list(self._records.values())

    def __contains__(self, ref: object) -> bool:
        return isinstance(ref, SourceRef | str) and self.get(ref) is not None

    def __iter__(self) -> Iterator[SourceRecord]:
        return iter(self.all())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

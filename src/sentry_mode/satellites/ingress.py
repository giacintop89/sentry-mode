"""The one door events come in by, and the decisions taken at it.

Everything a satellite says arrives here before anything else in the hub hears about it.
This module answers three questions, in this order: may this node speak at all, is this
message well formed, and is what it describes happening now. Only the last of those makes
an event eligible to reach a rule; everything else is recorded and set aside with a reason.

Being recorded is not the same as being acted on. A duplicate, a replay from a spool, the
opening snapshot of a sensor and a reading from a board whose clock has jumped are all
kept, because the history is worth having, and none of them is allowed to look like
something that is happening now.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import ValidationError

from sentry_mode.satellites.config import LimitsConfig
from sentry_mode.satellites.protocol import EventEnvelope
from sentry_mode.satellites.store import (
    DUPLICATE,
    ID_REUSED,
    SEQUENCE_REUSED,
    STORED,
    Entry,
    Mark,
    Store,
    StoreUnavailable,
)
from sentry_mode.sources.models import ClockStatus, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

log = logging.getLogger(__name__)

LIVE = "live"
INITIAL = "initial"
HISTORIC = "historic"

REASONS = (
    "duplicate",
    "retained",
    "initial_state",
    "replayed",
    "expired",
    "unapproved",
    "time_uncertain",
    "rate_limited",
    "out_of_order",
    "id_reused",
    "sequence_reused",
    "invalid_envelope",
    "identity_mismatch",
    "unregistered_source",
    "source_disabled",
    "store_unavailable",
    "queue_full",
)
"""Every reason an event exists in the journal without being acted on. The list is closed
so that a refusal cannot be invented in one branch and go uncounted everywhere else."""


@dataclass(frozen=True)
class NormalizedEvent:
    """An event with the hub's own answers attached: who it really is, and when it arrived.

    The wire event is what a node said. This is what the hub knows: the identity resolved
    from the topic, the zone from the registry rather than the payload, both of the hub's
    clocks, and whether the event is eligible to reach a rule.
    """

    ref: SourceRef
    event_id: str
    boot_id: str
    sequence: int
    kind: str
    value: bool | int | float | str | None
    unit: str | None
    quality: str
    clock_status: str
    zone: str | None
    occurred_at: datetime
    received_at: float
    received_monotonic: float
    queued_ms: int
    hub_epoch: int
    connection_id: str | None
    classification: str
    eligible: bool
    reason: str | None

    @property
    def node_id(self) -> str:
        return self.ref.node or ""

    @property
    def source_id(self) -> str:
        return self.ref.name


@dataclass(frozen=True)
class Receipt:
    """What the door did with one message, and whether the sender may stop holding it."""

    classification: str
    eligible: bool = False
    stored: bool = False
    reason: str | None = None
    event: NormalizedEvent | None = None
    settle: bool = True
    """Whether the satellite may be told to let this message go. It is false only when the
    hub failed to write it down, because a message the hub lost is one worth sending again."""


class RateLimiter:
    """A bucket for each node and one for everybody, refilled by the clock.

    A node is stopped by its own bucket long before the shared one empties, which is the
    point: one board stuck in a loop should cost the hub some counters, not everyone else's
    events.
    """

    def __init__(self, limits: LimitsConfig, *, clock=time.monotonic) -> None:
        self._per_node = float(limits.events_per_minute)
        self._total = float(limits.total_events_per_minute)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._shared = (self._total, clock())

    def _draw(
        self, level: float, filled_at: float, capacity: float, now: float
    ) -> tuple[bool, float]:
        level = min(capacity, level + (now - filled_at) * capacity / 60.0)
        if level < 1.0:
            return False, level
        return True, level - 1.0

    def allow(self, node_id: str) -> str | None:
        """None when the event may pass, otherwise which budget it ran out of."""
        now = self._clock()
        level, filled_at = self._buckets.get(node_id, (self._per_node, now))
        ok, level = self._draw(level, filled_at, self._per_node, now)
        if not ok:
            self._buckets[node_id] = (level, now)
            return "node"
        shared_level, shared_at = self._shared
        shared_ok, shared_level = self._draw(shared_level, shared_at, self._total, now)
        if not shared_ok:
            self._shared = (shared_level, now)
            return "hub"
        self._buckets[node_id] = (level, now)
        self._shared = (shared_level, now)
        return None


class EventIngress:
    """Validates, classifies and journals one event at a time."""

    def __init__(
        self,
        *,
        store: Store,
        sources: SourceRegistry,
        limits: LimitsConfig,
        zone_of=lambda node_id: None,
        clock=time.time,
        monotonic=time.monotonic,
    ) -> None:
        self.store = store
        self.sources = sources
        self.limits = limits
        self._zone_of = zone_of
        self._clock = clock
        self._monotonic = monotonic

    # -- the decision ------------------------------------------------------------

    def accept(self, node_id: str, document: dict, *, retained: bool = False) -> Receipt:
        """Decide what one message is, write it down, and say whether it may be acted on."""
        try:
            envelope = EventEnvelope.model_validate(document)
        except ValidationError as error:
            # The payload is not logged: it is unvalidated input, and it may quote a person.
            log.warning(
                "an event from %s does not match the contract (%d problems)",
                node_id,
                error.error_count(),
            )
            return Receipt(classification="rejected", reason="invalid_envelope")
        event, delivery = envelope.event, envelope.delivery
        if event.node_id != node_id:
            return Receipt(classification="rejected", reason="identity_mismatch")
        try:
            ref = SourceRef(node=node_id, name=event.source_id)
        except ValueError:
            return Receipt(classification="rejected", reason="unregistered_source")
        record = self.sources.get(ref)
        if record is None:
            return Receipt(classification="rejected", reason="unregistered_source")
        if record.state is SourceState.DISABLED:
            return Receipt(classification="rejected", reason="source_disabled")

        now, monotonic = self._clock(), self._monotonic()
        try:
            mark = self._position(node_id, event.source_id, str(event.boot_id))
        except StoreUnavailable as error:
            log.error("the journal cannot say where %s had got to: %s", ref.id, error)
            return Receipt(classification="rejected", reason="store_unavailable", settle=False)
        classification, reason, mark = self._classify(
            event=event, delivery=delivery, mark=mark, retained=retained, now=now
        )
        normalized = NormalizedEvent(
            ref=ref,
            event_id=str(event.event_id),
            boot_id=str(event.boot_id),
            sequence=event.sequence,
            kind=event.kind,
            value=event.value,
            unit=event.unit,
            quality=event.quality.value,
            clock_status=event.clock_status.value,
            zone=record.zone or self._zone_of(node_id),
            occurred_at=event.occurred_at,
            received_at=now,
            received_monotonic=monotonic,
            queued_ms=delivery.queued_ms,
            hub_epoch=delivery.hub_epoch,
            connection_id=str(delivery.connection_id),
            classification=classification,
            eligible=reason is None,
            reason=reason,
        )
        return self._write(normalized, mark)

    def _position(self, node_id: str, source_id: str, boot_id: str) -> Mark:
        """Where this source had got to, starting again when the board has restarted."""
        known = self.store.mark(node_id, source_id)
        if known is None or known.boot_id != boot_id:
            return Mark(node_id=node_id, source_id=source_id, boot_id=boot_id)
        return known

    def _classify(self, *, event, delivery, mark: Mark, retained: bool, now: float):
        """Say what this event is. Only one answer, `live`, may reach a rule."""
        moment = event.occurred_at.astimezone(timezone.utc).timestamp()
        age = now - moment
        # What this reading says about its own clock, and whether the source was already
        # under suspicion. A new baseline is judged on the first alone: that is how a
        # source gets out of suspicion at all.
        clock_bad = event.clock_status is not ClockStatus.SYNCED or age < -float(
            self.limits.future_tolerance_seconds
        )
        uncertain = clock_bad or mark.time_state == "uncertain"
        ahead = max(mark.high_water, event.sequence)

        if retained:
            return HISTORIC, "retained", mark
        if delivery.initial_state:
            # A snapshot is how a source says what it already was. It is the baseline that
            # later readings are compared against, and it clears an earlier clock fault:
            # the source starts again from here rather than replaying what it missed.
            fresh = Mark(
                node_id=mark.node_id,
                source_id=mark.source_id,
                boot_id=mark.boot_id,
                high_water=ahead,
                baseline_taken=True,
                time_state="uncertain" if clock_bad else "ok",
                last_occurred_at=event.occurred_at.isoformat(),
            )
            return INITIAL, "initial_state", fresh
        moved = Mark(
            node_id=mark.node_id,
            source_id=mark.source_id,
            boot_id=mark.boot_id,
            high_water=ahead,
            baseline_taken=mark.baseline_taken,
            time_state="uncertain" if uncertain else mark.time_state,
            last_occurred_at=event.occurred_at.isoformat(),
        )
        if delivery.replayed:
            return HISTORIC, "replayed", moved
        if uncertain:
            # Until the source says where it is starting from again, its readings are
            # visible and nothing is founded on how fresh they are.
            return HISTORIC, "time_uncertain", moved
        if age > float(self.limits.accept_within_seconds):
            return HISTORIC, "expired", moved
        if event.sequence <= mark.high_water:
            # Older than something already seen in this boot: history, and it does not drag
            # the source's current state backwards.
            return HISTORIC, "out_of_order", moved
        return LIVE, None, moved

    # -- writing it down ---------------------------------------------------------

    def _write(self, event: NormalizedEvent, mark: Mark) -> Receipt:
        """Commit before anything is acknowledged, and say so if the commit did not happen."""
        entry = Entry(
            node_id=event.node_id,
            source_id=event.source_id,
            event_id=event.event_id,
            boot_id=event.boot_id,
            sequence=event.sequence,
            kind=event.kind,
            value=event.value,
            unit=event.unit,
            quality=event.quality,
            clock_status=event.clock_status,
            zone=event.zone,
            occurred_at=event.occurred_at.isoformat(),
            received_at=event.received_at,
            received_monotonic=event.received_monotonic,
            queued_ms=event.queued_ms,
            hub_epoch=event.hub_epoch,
            connection_id=event.connection_id,
            classification=event.classification,
            eligible=event.eligible,
            reason=event.reason,
        )
        try:
            outcome = self.store.admit(entry, mark=mark)
        except StoreUnavailable as error:
            # Nothing was written, so nothing is acknowledged: the node keeps its copy and
            # this is a fault an operator has to see, not an event quietly discarded.
            log.error("an event from %s could not be journalled: %s", event.node_id, error)
            return Receipt(classification="rejected", reason="store_unavailable", settle=False)
        if outcome == STORED:
            return Receipt(
                classification=event.classification,
                eligible=event.eligible,
                stored=True,
                reason=event.reason,
                event=event if event.eligible else None,
            )
        if outcome == ID_REUSED:
            # The same identifier over different contents is not a retry. Nothing is
            # overwritten and the collision is reported.
            log.warning(
                "%s sent a second, different event under the identifier %s",
                event.node_id,
                event.event_id,
            )
        return Receipt(classification=HISTORIC, reason=_REFUSALS[outcome])


_REFUSALS = {
    DUPLICATE: "duplicate",
    ID_REUSED: "id_reused",
    SEQUENCE_REUSED: "sequence_reused",
}


__all__ = [
    "HISTORIC",
    "INITIAL",
    "LIVE",
    "REASONS",
    "EventIngress",
    "NormalizedEvent",
    "RateLimiter",
    "Receipt",
]

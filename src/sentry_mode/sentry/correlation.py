"""A sensor event confirmed by a camera: the one correlation a rule can ask for.

Like the other triggers, nothing here runs an action or touches hardware. The functions
take what was observed and the rule's state, update the state, and return what happened
as `(kind, message)` pairs. The engine and the simulation use the same code.

How a sequence behaves:

- A valid sensor event in the right direction opens a *candidate*: a window of
  `within_seconds` on the hub's clock. There is at most one per rule.
- Another sensor event while a candidate is open does not move its deadline. A PIR that
  keeps firing would otherwise keep a window open for as long as somebody walks past,
  and "within five seconds" would stop meaning anything. The window runs out, and the
  next event after that opens a new one.
- Only camera samples the hub received after the event count, and they have to add up to
  the detection's consecutive samples inside the window. A sighting before the event is
  not a confirmation of it.
- The confirmation is used once: firing closes the candidate. The camera side then stays
  latched, as a camera rule does, until the object has really been out of sight for
  `rearm_after_absence_seconds`. Frames that did not arrive are not an absence.
- A doubtful reading from the sensor, a gap in the camera's samples, or the rule being
  paused drop the candidate. A candidate that has run out is never confirmed late.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentry_mode.sentry.config import SequenceTrigger
from sentry_mode.sentry.triggers import Observed, RuleState, cooled, is_for, sensor_fires

Notes = list[tuple[str, str]]


@dataclass(frozen=True)
class Candidate:
    """A sensor event waiting for the camera to confirm it."""

    opened: float
    """When the hub received the event, on its monotonic clock."""
    deadline: float
    event_id: str
    zone: str | None


def expire(trigger: SequenceTrigger, state: RuleState, now: float) -> Notes:
    """Close a candidate whose window has run out by `now`."""
    candidate = state.candidate
    if candidate is None or now <= candidate.deadline:
        return []
    state.candidate, state.hits = None, 0
    vision = trigger.vision
    return [
        (
            "expired",
            f"No {vision.object} on {vision.source_id} within "
            f"{trigger.within_seconds:g} s of {trigger.sensor.source_id}; nothing was done.",
        )
    ]


def drop(state: RuleState, reason: str) -> Notes:
    """Close a candidate because what it relied on can no longer be trusted."""
    if state.candidate is None:
        return []
    state.candidate, state.hits = None, 0
    return [("cancelled", f"The waiting sequence was dropped: {reason}.")]


def sequence_event(
    trigger: SequenceTrigger, state: RuleState, event: Observed, camera_zone: str | None = None
) -> Notes:
    """A sensor event through a sequence rule. Opens a candidate, or explains why not."""
    sensor = trigger.sensor
    if not is_for(sensor, event):
        return []
    now = event.received_monotonic
    notes = expire(trigger, state, now)
    if event.quality != "valid":
        return notes + drop(state, f"{sensor.source_id} reported a {event.quality} reading")
    if not sensor_fires(sensor, event):
        return notes
    if now <= state.opened_until:
        return notes  # older than a window already opened: arrived out of order
    if state.candidate is not None:
        return notes  # the open window keeps its deadline
    vision = trigger.vision
    zone = getattr(event, "zone", None)
    if trigger.same_zone and None not in (zone, camera_zone) and zone != camera_zone:
        return notes + [
            (
                "skipped",
                f"{sensor.source_id} is now in zone {zone}, "
                f"not {camera_zone} where {vision.source_id} is.",
            )
        ]
    if state.latched:
        return notes + [
            (
                "skipped",
                f"{event.kind} from {sensor.source_id}, but the {vision.object} confirmed last "
                f"time has not left {vision.source_id} yet.",
            )
        ]
    state.candidate = Candidate(
        opened=now,
        deadline=now + trigger.within_seconds,
        event_id=str(getattr(event, "event_id", "")),
        zone=zone,
    )
    state.opened_until = now
    state.hits = 0
    return notes + [
        (
            "waiting",
            f"{event.kind} from {sensor.source_id}; waiting up to "
            f"{trigger.within_seconds:g} s for a {vision.object} on {vision.source_id}.",
        )
    ]


def sequence_sample(
    trigger: SequenceTrigger, state: RuleState, count: int, captured: float, cooldown: float
) -> Notes:
    """One camera sample through a sequence rule. Fires when it completes the confirmation."""
    vision = trigger.vision
    notes = expire(trigger, state, captured)
    if count < vision.min_count:
        state.hits = 0
        if state.absent_since is None:
            state.absent_since = captured
        if state.latched and captured - state.absent_since >= vision.rearm_after_absence_seconds:
            state.latched = False
            notes.append(("rearmed", "Object absent long enough; ready for another sequence."))
        return notes
    state.absent_since = None
    candidate = state.candidate
    if state.latched or candidate is None or captured <= candidate.opened:
        state.hits = 0
        return notes
    state.hits = min(vision.consecutive_detections, state.hits + 1)
    if state.hits == 1:
        notes.append(("detected", f"{vision.object}: {count} matching object(s)."))
    if state.hits < vision.consecutive_detections:
        return notes
    state.candidate, state.hits = None, 0
    if not cooled(state, cooldown, captured):
        notes.append(("skipped", "Confirmed, but still cooling down from the last time."))
        return notes
    state.latched, state.last_trigger = True, captured
    notes.append(
        (
            "triggered",
            f"{vision.object} on {vision.source_id} confirmed "
            f"{captured - candidate.opened:.1f} s after {trigger.sensor.source_id}.",
        )
    )
    return notes


__all__ = ["Candidate", "drop", "expire", "sequence_event", "sequence_sample"]

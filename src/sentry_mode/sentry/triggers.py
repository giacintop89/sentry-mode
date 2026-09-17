"""When a rule goes off: one small state machine per kind of trigger.

Nothing here runs an action or touches hardware. Each function takes what was observed
and the rule's state, updates the state, and returns whether the rule fired. That makes
the same code usable when armed and in a simulation, where the state belongs to the
simulation and nothing is executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from sentry_mode.sentry.config import (
    AudioEventTrigger,
    PresenceStateTrigger,
    SensorEventTrigger,
    ThresholdTrigger,
    VisionTrigger,
)
from sentry_mode.sources.models import SourceRef
from sentry_mode.vision.detection import Detection

if TYPE_CHECKING:
    from sentry_mode.sentry.correlation import Candidate


class Observed(Protocol):
    """The parts of a normalized satellite event a trigger looks at."""

    @property
    def ref(self) -> SourceRef: ...
    @property
    def kind(self) -> str: ...
    @property
    def value(self) -> object: ...
    @property
    def quality(self) -> str: ...
    @property
    def received_monotonic(self) -> float: ...


@dataclass
class RuleState:
    """Where one rule stands. Only the fields its trigger uses ever change."""

    hits: int = 0
    latched: bool = False
    absent_since: float | None = None
    last_trigger: float = float("-inf")
    phase: str = "unknown"
    """For thresholds: `unknown` until the first good reading, then `clear`, `pending`
    (past the limit, waiting out `for_seconds`) or `active` (fired, or already past the
    limit when watching began). For presence: where the device stands across its
    observers, `unknown` until they agree."""
    pending_since: float | None = None
    seen: dict[str, str] = field(default_factory=dict)
    """For presence: what each observer last said about the device."""
    candidate: Candidate | None = None
    """For sequences: the sensor event waiting for the camera, if any."""
    opened_until: float = float("-inf")
    """For sequences: when the newest window opened. An event older than that is late."""


def cooled(state: RuleState, cooldown: float, now: float) -> bool:
    return now - state.last_trigger >= cooldown


# -- vision ------------------------------------------------------------------------------


def detection_matches(trigger: VisionTrigger, detection: Detection) -> bool:
    if detection.label != trigger.object or detection.confidence < trigger.min_confidence:
        return False
    if trigger.region is None:
        return True
    x1, y1, x2, y2 = detection.box
    left, top, right, bottom = trigger.region
    return left <= (x1 + x2) / 2 <= right and top <= (y1 + y2) / 2 <= bottom


# -- satellite events --------------------------------------------------------------------


def is_for(
    trigger: SensorEventTrigger | ThresholdTrigger | AudioEventTrigger, event: Observed
) -> bool:
    return event.ref.id == trigger.source_id and event.kind == trigger.kind


def sensor_fires(trigger: SensorEventTrigger, event: Observed) -> bool:
    """A reported change in the direction the rule asks for, from a reading worth trusting.

    Each event from a sensor already is a change: the agent reports edges, not samples.
    Its opening snapshot never reaches here, because the hub does not treat a snapshot as
    news. A reading whose quality is anything but `valid` is neither edge.
    """
    if not is_for(trigger, event) or event.quality != "valid":
        return False
    if not isinstance(event.value, bool):
        return False
    if trigger.edge == "rising":
        return event.value is True
    if trigger.edge == "falling":
        return event.value is False
    return True


def sound_fires(trigger: AudioEventTrigger, event: Observed) -> bool:
    """A microphone that has just become loud. Its going quiet again is not news."""
    return is_for(trigger, event) and event.quality == "valid" and event.value is True


PRESENT, ABSENT, UNKNOWN = "present", "absent", "unknown"


def presence_fires(trigger: PresenceStateTrigger, state: RuleState, event: Observed) -> bool:
    """Where the device stands, put together from every observer the rule names.

    Present as soon as one observer says so; absent only when they all do. An observer
    that has said nothing yet, or whose scanner cannot see, holds the answer at unknown,
    and unknown sets nothing off.
    """
    watching = (trigger.source_id, *trigger.observers)
    if event.ref.id not in watching or event.kind != trigger.kind:
        return False
    said = event.value if event.quality == "valid" and event.value in (PRESENT, ABSENT) else UNKNOWN
    state.seen[event.ref.id] = str(said)
    said_by = [state.seen.get(one, UNKNOWN) for one in watching]
    if PRESENT in said_by:
        combined = PRESENT
    elif all(one == ABSENT for one in said_by):
        combined = ABSENT
    else:
        combined = UNKNOWN
    before, state.phase = state.phase, combined
    return combined != before and combined == trigger.state


def _number(event: Observed) -> float | None:
    value = event.value
    if event.quality != "valid" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def threshold_fires(trigger: ThresholdTrigger, state: RuleState, event: Observed) -> bool:
    """Past the limit for long enough, then silent until the value is well back.

    A missing or doubtful reading is not a zero. It changes nothing, except that a wait in
    progress starts again, because the time the value was past the limit is no longer known.
    """
    if not is_for(trigger, event):
        return False
    value = _number(event)
    if value is None:
        if state.phase == "pending":
            state.phase, state.pending_since = "clear", None
        return False
    if trigger.above is not None:
        past = value > trigger.above
        released = value <= trigger.above - trigger.hysteresis
    else:
        assert trigger.below is not None
        past = value < trigger.below
        released = value >= trigger.below + trigger.hysteresis
    now = event.received_monotonic

    if state.phase == "unknown":
        # The first reading is where things stand. A value already past the limit when
        # watching began did not just cross it.
        state.phase = "active" if past else "clear"
        return False
    if state.phase == "active":
        if released:
            state.phase = "clear"
        return False
    if not past:
        state.phase, state.pending_since = "clear", None
        return False
    if state.phase == "clear":
        state.phase, state.pending_since = "pending", now
    assert state.pending_since is not None
    if now - state.pending_since >= trigger.for_seconds:
        state.phase, state.pending_since = "active", None
        return True
    return False


__all__ = [
    "RuleState",
    "cooled",
    "detection_matches",
    "is_for",
    "sensor_fires",
    "threshold_fires",
]

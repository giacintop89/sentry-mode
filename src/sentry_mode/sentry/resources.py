"""What arming a set of rules needs, worked out before anything is started.

The first version of Sentry always started the camera and the detector, because every
rule was about the camera. A rule set off by a PIR needs neither. A PIR rule that takes a
photo needs the camera but not the detector. The planner works this out from the rules
and the source registry. Anything missing is reported as a named problem before arming,
never discovered afterwards.

Two kinds of need are kept apart. A source a trigger watches has to be running for the
whole time Sentry is armed. A source an action uses has to exist, be enabled and be
reachable, but nothing is watching it.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass, field
from typing import TypeVar

from sentry_mode.sentry.config import (
    ARMABLE_TRIGGERS,
    TRIGGER_SOURCE,
    Action,
    AudioAction,
    HealthEventTrigger,
    PhotoAction,
    RuleV2,
    SensorEventTrigger,
    ThresholdTrigger,
    VideoAction,
    VisionTrigger,
)
from sentry_mode.sources.models import (
    PRIMARY_CAMERA,
    PRIMARY_MICROPHONE,
    SourceKind,
    SourceRef,
    SourceState,
)
from sentry_mode.sources.registry import SourceRegistry

A = TypeVar("A")


def resolve(rule: RuleV2, action: A) -> A:
    """The action with `trigger_source` replaced by the camera that set the rule off.

    Only a camera trigger names a camera. For any other trigger the action is returned as
    it is, and the planner refuses it.
    """
    if (
        isinstance(action, (PhotoAction, VideoAction))
        and action.source_id == TRIGGER_SOURCE
        and isinstance(rule.trigger, VisionTrigger)
    ):
        return action.model_copy(update={"source_id": rule.trigger.source_id})  # type: ignore[return-value]
    return action


def resolved(rule: RuleV2) -> list[Action]:
    return [resolve(rule, action) for action in rule.actions]


@dataclass(frozen=True)
class Problem:
    rule_id: str
    rule_name: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule_name}: {self.message}"


@dataclass(frozen=True)
class Plan:
    detector: bool = False
    """A trigger watches this node's camera for objects, so the model has to be loaded."""
    camera: bool = False
    """This node's camera has to run while armed: for the detector, or so a photo is ready."""
    min_confidence: float = 0.7
    vision: dict[str, float] = field(default_factory=dict)
    """Other cameras on this hub watched for objects, with the lowest confidence each needs."""
    ready: frozenset[str] = frozenset()
    """Other cameras kept streaming while armed because an action takes pictures from them."""
    watched: frozenset[str] = frozenset()
    """Satellite sources a trigger listens to."""
    needs: dict[str, frozenset[str]] = field(default_factory=dict)
    """Every source each rule depends on, by rule ID, for isolating a fault to its rules."""
    problems: tuple[Problem, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    def rules_needing(self, source_id: str) -> set[str]:
        return {rule_id for rule_id, sources in self.needs.items() if source_id in sources}

    def as_dict(self) -> dict:
        return {
            "detector": self.detector,
            "camera": self.camera,
            "vision": sorted(self.vision),
            "ready": sorted(self.ready),
            "watched": sorted(self.watched),
            "problems": [
                {"rule_id": p.rule_id, "rule": p.rule_name, "message": p.message}
                for p in self.problems
            ],
            "notes": list(self.notes),
        }


EXPECTED = {
    "vision": SourceKind.CAMERA,
    "sensor_event": SourceKind.SENSOR,
    "threshold": SourceKind.SENSOR,
}


class ResourcePlanner:
    def __init__(
        self,
        sources: SourceRegistry,
        *,
        satellites_enabled: bool,
        cameras: Container[str] = (),
    ) -> None:
        self.sources = sources
        self.satellites_enabled = satellites_enabled
        self.cameras = cameras
        """Cameras besides the primary one that this hub can drive itself."""

    def plan(self, rules: list[RuleV2]) -> Plan:
        problems: list[Problem] = []
        needs: dict[str, frozenset[str]] = {}
        watched: set[str] = set()
        detector = camera_for_actions = False
        confidences = []
        vision: dict[str, float] = {}
        ready: set[str] = set()
        notes: list[str] = []

        for rule in rules:

            def fail(message: str, rule: RuleV2 = rule) -> None:
                problems.append(Problem(rule.id, rule.name, message))

            used: set[str] = set()
            trigger = rule.trigger
            if trigger.type not in ARMABLE_TRIGGERS:
                fail(f"{trigger.type.replace('_', ' ')} triggers are not available yet")
            if isinstance(trigger, HealthEventTrigger):
                pass  # raised by the hub about a node, so it never depends on that node
            else:
                used.add(trigger.source_id)
                self._check(trigger.source_id, EXPECTED.get(trigger.type), fail)
            if isinstance(trigger, VisionTrigger):
                if trigger.source_id == PRIMARY_CAMERA:
                    detector = True
                    confidences.append(trigger.min_confidence)
                elif trigger.source_id in self.cameras:
                    vision[trigger.source_id] = min(
                        trigger.min_confidence, vision.get(trigger.source_id, 1.0)
                    )
                else:
                    fail("watching a satellite camera is not available yet")
            elif isinstance(trigger, (SensorEventTrigger, ThresholdTrigger)):
                watched.add(trigger.source_id)

            if self._actions(rule, used, fail, ready, notes):
                camera_for_actions = True
            needs[rule.id] = frozenset(used)

        if camera_for_actions and not detector:
            notes.append(
                "The camera runs while Sentry is armed so that a photo or video can start "
                "at once; the object detector is not loaded."
            )
        return Plan(
            detector=detector,
            camera=detector or camera_for_actions,
            min_confidence=min(confidences, default=0.7),
            vision=vision,
            ready=frozenset(ready - set(vision)),
            watched=frozenset(watched),
            needs=needs,
            problems=tuple(problems),
            notes=tuple(notes),
        )

    def plan_actions(self, rule: RuleV2) -> list[Problem]:
        """What stands in the way of running a rule's actions now, as a test does."""
        problems: list[Problem] = []

        def fail(message: str) -> None:
            problems.append(Problem(rule.id, rule.name, message))

        self._actions(rule, set(), fail, set(), [])
        return problems

    def _actions(
        self, rule: RuleV2, used: set[str], fail, ready: set[str], notes: list[str]
    ) -> bool:
        """Check the sources a rule's actions use. True if they need this node's camera.

        Cameras besides this node's own that an action records from go into `ready`.
        """
        camera = False
        for original in rule.actions:
            action = resolve(rule, original)
            if isinstance(action, (PhotoAction, VideoAction)):
                if action.source_id == TRIGGER_SOURCE:
                    fail(
                        f"a {action.type} after a {rule.trigger.type.replace('_', ' ')} "
                        "trigger has to name its camera"
                    )
                    continue
                used.add(action.source_id)
                if self._check(action.source_id, SourceKind.CAMERA, fail):
                    if action.source_id == PRIMARY_CAMERA:
                        camera = True
                    elif action.source_id in self.cameras:
                        ready.add(action.source_id)
                    else:
                        fail(f"{action.source_id} is not a camera this hub can record from")
            if isinstance(action, VideoAction) and action.audio and action.microphone is None:
                note = (
                    f"{rule.name}: the video from {action.source_id} is recorded without "
                    "sound, because no microphone is chosen for it."
                )
                if note not in notes:
                    notes.append(note)
            microphone = (
                action.audio_source_id
                if isinstance(action, AudioAction)
                else action.microphone
                if isinstance(action, VideoAction)
                else None
            )
            if microphone is not None:
                used.add(microphone)
                if self._check(microphone, SourceKind.MICROPHONE, fail):
                    if not SourceRef.parse(microphone).is_local:
                        fail("recording from a satellite microphone is not available yet")
                    elif microphone != PRIMARY_MICROPHONE:
                        fail(f"sound is recorded only from {PRIMARY_MICROPHONE} for now")
        return camera

    def _check(self, source_id: str, kind: SourceKind | None, fail) -> bool:
        """Whether a source can be used, reporting the reason when it cannot."""
        ref = SourceRef.parse(source_id)
        if not ref.is_local and not self.satellites_enabled:
            fail(f"{source_id} is on a satellite, and satellites are switched off")
            return False
        record = self.sources.get(ref)
        if record is None:
            fail(f"{source_id} is not a known source")
            return False
        if kind is not None and record.kind is not kind:
            fail(f"{source_id} is a {record.kind.value}, not a {kind.value}")
            return False
        if record.state is SourceState.DISABLED:
            fail(f"{source_id} is disabled")
            return False
        return True


__all__ = ["Plan", "Problem", "ResourcePlanner", "resolve", "resolved"]

"""Confirmed triggers, a bounded action queue, and explicit arm/disarm lifecycle.

A rule is set off by a camera, a sensor or a measurement, and its actions run in order
from a bounded queue. Every queued action carries the context it was decided in, so an
action left over from an earlier arming is dropped rather than run late.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from pydantic import SecretStr

from sentry_mode.audio.remote import MicrophoneTable
from sentry_mode.audio.sounds import SoundLibrary
from sentry_mode.audio.speech import speak
from sentry_mode.audio.tunes import TUNES, play_tune
from sentry_mode.config import Settings
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.microphone import Microphone
from sentry_mode.sentry.config import (
    AudioAction,
    AudioEventTrigger,
    PhotoAction,
    PresenceStateTrigger,
    Rule,
    RuleV2,
    SensorEventTrigger,
    SentryConfig,
    SentryConfigV2,
    SequenceTrigger,
    SoundAction,
    SSHAction,
    SSHCommand,
    TelegramAction,
    ThresholdTrigger,
    TTSAction,
    TuneAction,
    VideoAction,
    VisionTrigger,
    WaitAction,
)
from sentry_mode.sentry.context import ActionContext
from sentry_mode.sentry.correlation import drop, expire, sequence_event, sequence_sample
from sentry_mode.sentry.migration import (
    LEGACY_SCHEMA,
    SchemaUpgradeRequired,
    dump_v1,
    obstacles,
    read_document,
    rule_to_v2,
    slug,
    to_v1,
    to_v2,
    write_document,
)
from sentry_mode.sentry.resources import DETECTOR, Plan, ResourcePlanner, resolved
from sentry_mode.sentry.triggers import (
    RuleState,
    cooled,
    detection_matches,
    presence_fires,
    sensor_fires,
    sound_fires,
    threshold_fires,
)
from sentry_mode.sources.legacy import register_legacy
from sentry_mode.sources.manager import Lease, SourceManager
from sentry_mode.sources.models import (
    PRIMARY_CAMERA,
    PRIMARY_MICROPHONE,
    SourceRef,
    SourceState,
)
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.detection import Detection
from sentry_mode.vision.labels import CLASSES
from sentry_mode.vision.recording import Captures, SoundSource
from sentry_mode.vision.stream import VideoStream

logger = logging.getLogger(__name__)


Action = (
    WaitAction
    | PhotoAction
    | AudioAction
    | VideoAction
    | TTSAction
    | TuneAction
    | SoundAction
    | SSHAction
    | TelegramAction
)


MOTION_KINDS = frozenset({"sensor.motion", "motion.pir"})
BOOST_FACTOR = 2.0
BOOST_SECONDS = 10.0
FRESH_SECONDS = 2.0
"""A picture older than this is not evidence of what is happening now."""
FRAME_WAIT_SECONDS = 3.0
ECHO_SECONDS = 2.0
"""How long after the hub stops playing sound a microphone is still taken to be hearing it."""
READY_FPS = 5.0
"""The rate a camera kept ready for photos runs at, when nothing else asks for more."""


class Unavailable(HardwareError):
    """The camera an action names has nothing recent to give. No other camera stands in."""


class _Others:
    """The cameras the planner may watch besides the primary one, read when it plans."""

    def __init__(self, cameras: SourceManager) -> None:
        self.cameras = cameras

    def __contains__(self, source_id: object) -> bool:
        return source_id != PRIMARY_CAMERA and source_id in self.cameras


@dataclass
class Job:
    """One group of a rule's sequence: its steps start together, and the queue keeps the
    groups in order, so the next group only starts once this one has finished."""

    rule: str
    steps: list[Action]
    created: float
    delay: float = 0.0
    context: ActionContext | None = None


class Sentry:
    def __init__(
        self,
        settings: Settings,
        video: VideoStream,
        audio_lock,
        sources: SourceRegistry | None = None,
        cameras: SourceManager | None = None,
        microphones: MicrophoneTable | None = None,
    ):
        self.settings, self.video, self.audio_lock = settings, video, audio_lock
        self.guard = threading.RLock()
        self.lifecycle = threading.Lock()
        self.config: SentryConfigV2 = to_v2(settings.sentry)
        self.schema_version = LEGACY_SCHEMA
        self.sources = (
            sources if sources is not None else register_legacy(SourceRegistry(), settings)
        )
        self.cameras = cameras if cameras is not None else SourceManager(self.sources)
        self.microphones = microphones if microphones is not None else MicrophoneTable()
        """Satellite microphones this hub can record from."""
        self.planner = ResourcePlanner(
            self.sources,
            satellites_enabled=settings.satellites.enabled,
            cameras=_Others(self.cameras),
            microphones=self.microphones,
        )
        self.freshness: Callable[[str], str] | None = None
        """How recently a satellite node proved it was there, when satellites are on."""
        self.watching: dict[str, Lease] = {}
        """Leases on other cameras: watched for objects, or kept ready for photos."""
        self.halted: deque[str] = deque(maxlen=64)
        """Triggers whose remaining steps were dropped by a step's `if_unavailable: stop`."""
        self.plan: Plan | None = None
        self.arm_epoch = 0
        self.suspended: dict[str, str] = {}
        self.revision = 0
        self.config_error: str | None = None
        self.error: str | None = None
        self.armed = False
        self.armed_at = 0.0
        self.cancelled = threading.Event()
        self.cancelled.set()
        self.thread: threading.Thread | None = None
        self.jobs: queue.Queue[Job] = queue.Queue(maxsize=16)
        self.events: deque[dict] = deque(maxlen=200)
        self.event_id = 0
        self.states: dict[str, RuleState] = {}
        self.samples: dict[str, float] = {}
        """The capture time of the last sample from each camera, in this arming."""
        self.active_labels: dict[int, str] = {}
        self.captures = Captures(settings.captures_directory)
        self.sounds = SoundLibrary(settings.sounds_directory)
        self.recorders: list[threading.Thread] = []
        self.quiet_from = 0.0
        """When this hub's own sound last stopped, for telling an echo from an intruder."""
        self._load()
        self.video.detection.on_result = self.observe
        self.video.detection.on_error = self.fault
        self.video.on_error = self.fault

    def fault(self, message: str):
        with self.guard:
            if not self.armed:
                return
            if self.config.fault_policy == "isolated" and self.plan is not None:
                # The worker's next health check pauses only the rules that need it.
                logger.warning("Sentry source failed: %s", message)
                return
            self.error = message
            self.armed = False
            self.cancelled.set()
            self._discard_jobs()
            self._event("fault", message)

    def _load(self):
        path = self.settings.sentry_state_file
        if not path.exists():
            return
        try:
            if path.stat().st_size > 262144:
                raise ValueError("Saved Sentry configuration is too large.")
            document = read_document(json.loads(path.read_text()))
            self.config = document.config
            self.revision = document.revision
            self.schema_version = document.schema_version
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.config_error = f"Saved Sentry configuration could not be loaded: {exc}"

    def configuration(self) -> dict:
        """The rules as the first version of the editor sees them, or a clear refusal.

        When the rules use anything the first version cannot express, this raises
        `SchemaUpgradeRequired` rather than returning a partial copy.
        """
        with self.guard:
            config = dump_v1(to_v1(self.config))
            config["telegram"]["bot_token"] = ""
            return {
                "config": config,
                "telegram_token_configured": bool(
                    self.config.telegram.bot_token.get_secret_value()
                ),
                "revision": self.revision,
                "error": self.config_error,
                "objects": CLASSES,
            }

    def configuration_v2(self) -> dict:
        with self.guard:
            config = self.config.model_dump(mode="json")
            config["telegram"]["bot_token"] = ""
            enabled = [rule for rule in self.config.rules if rule.enabled]
            return {
                "schema_version": 2,
                "stored_schema_version": self.schema_version,
                "config": config,
                "telegram_token_configured": bool(
                    self.config.telegram.bot_token.get_secret_value()
                ),
                "revision": self.revision,
                "error": self.config_error,
                "objects": CLASSES,
                "plan": self.planner.plan(enabled).as_dict(),
                "sources": [
                    {
                        "source_id": record.id,
                        "kind": record.kind.value,
                        "display_name": record.display_name,
                        "zone": record.zone,
                        "state": record.state.value,
                    }
                    for record in self.sources.all()
                ],
            }

    def update(
        self, config: SentryConfig, revision: int, clear_telegram_token: bool = False
    ) -> dict:
        """Save rules from the first version of the editor.

        A first-version client cannot see rules it does not understand. If it were allowed
        to save, it would delete them, so it is refused while any such rule exists.
        """
        with self.guard:
            reasons = obstacles(self.config)
            if reasons:
                raise SchemaUpgradeRequired(reasons)
            converted = to_v2(config, previous=self.config)
        self.update_v2(converted, revision, clear_telegram_token)
        return self.configuration()

    def update_v2(
        self, config: SentryConfigV2, revision: int, clear_telegram_token: bool = False
    ) -> dict:
        with self.lifecycle, self.guard:
            if self.armed:
                raise BlockingIOError("Disarm Sentry before changing rules or actions.")
            if revision != self.revision:
                raise BlockingIOError("Configuration changed in another tab. Reload before saving.")
            config = config.model_copy(deep=True)
            if clear_telegram_token:
                config.telegram.bot_token = SecretStr("")
            elif not config.telegram.bot_token.get_secret_value():
                config.telegram.bot_token = self.config.telegram.bot_token
            self._persist(config, revision + 1)
            self._event("configured", "Rules and actions saved.")
            return self.configuration_v2()

    def set_test_mode(self, enabled: bool) -> dict:
        """Switch the node between logging actions and running them.

        Test mode covers every rule, so it is its own switch rather than part of a rule
        edit: it applies and is saved the moment it is toggled. Changing it while armed
        would move the node between rehearsal and real actions mid-run, so it is refused.
        """
        with self.lifecycle, self.guard:
            if self.armed:
                raise BlockingIOError("Disarm Sentry before changing test mode.")
            if self.config.test_mode is not enabled:
                self._persist(
                    self.config.model_copy(update={"test_mode": enabled}), self.revision + 1
                )
                self._event(
                    "configured",
                    "Test mode on; actions are only logged."
                    if enabled
                    else "Test mode off; actions run for real.",
                )
            return {"test_mode": self.config.test_mode, "revision": self.revision}

    def _persist(self, config: SentryConfigV2, revision: int) -> None:
        """Write the configuration and adopt it. The caller holds the guard.

        The file keeps the version it already has, so the previous release can still read
        an unmigrated file. A node with no file yet has nothing to preserve and is written
        in the newer version as soon as its rules need it. An existing first-version file
        is converted only by the migration script, which keeps a backup first.
        """
        path = self.settings.sentry_state_file
        version = self.schema_version
        if version == LEGACY_SCHEMA and obstacles(config):
            if path.exists():
                raise SchemaUpgradeRequired(
                    obstacles(config)
                    + ["run scripts/migrate_satellites.py --apply to convert the saved rules"]
                )
            version = 2
        document = write_document(config, revision, version)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
                name = Path(out.name)
                json.dump(document, out)
                out.flush()
                os.fsync(out.fileno())
            name.replace(path)
        finally:
            if name is not None:
                name.unlink(missing_ok=True)
        self.config, self.revision, self.config_error = config, revision, None
        self.schema_version = version

    def _check(self, rules: list[Rule] | list[RuleV2], logged_only: bool = False):
        """Refuse actions whose file, saved command or credentials are missing.

        Rules that will only be logged are allowed to lack Telegram credentials; rules
        whose actions are about to run are not.
        """
        for rule in rules:
            for action in rule.actions:
                if isinstance(action, SoundAction) and not self.sounds.exists(action.sound):
                    raise ValueError(
                        f"Rule {rule.name} plays an audio file that was deleted; "
                        "choose another file."
                    )
                if (
                    isinstance(action, SSHAction)
                    and action.command_id not in self.config.ssh_commands
                ):
                    raise ValueError(f"Unknown SSH command: {action.command_id}")
        if logged_only or not any(
            isinstance(action, TelegramAction) for rule in rules for action in rule.actions
        ):
            return
        if not (self.config.telegram.bot_token.get_secret_value() and self.config.telegram.chat_id):
            raise ValueError("Configure a Telegram bot token and chat ID first.")

    @staticmethod
    def _describe(action) -> str:
        """What test mode logs instead of running an action."""
        if isinstance(action, WaitAction):
            return f"Wait: {action.seconds:g} s"
        where = ""
        if isinstance(action, (PhotoAction, VideoAction)) and action.source_id != PRIMARY_CAMERA:
            where = " from " + action.source_id.replace("trigger_source", "the triggering camera")
        if isinstance(action, PhotoAction):
            return (
                f"Photos{where}: {action.count} every {action.interval_seconds:g} s"
                if action.count > 1
                else f"Photo{where}"
            )
        if isinstance(action, VideoAction):
            sound = action.microphone
            return (
                f"Video{where}: {action.duration_seconds} s"
                + (f" with sound from {sound}" if sound and sound != PRIMARY_MICROPHONE else "")
                + (" with sound" if sound == PRIMARY_MICROPHONE else "")
                + (" without sound" if action.audio and sound is None else "")
            )
        if isinstance(action, AudioAction):
            return f"Audio: {action.duration_seconds} s"
        if isinstance(action, TTSAction):
            return "TTS: " + action.text
        if isinstance(action, TuneAction):
            return (
                "Tune: "
                + TUNES[action.tune][0]
                + (f" x{action.repeat}" if action.repeat > 1 else "")
                + (f" at {action.pitch:+g} st" if action.pitch else "")
            )
        if isinstance(action, SoundAction):
            return (
                "Audio file: " + action.sound + (f" x{action.repeat}" if action.repeat > 1 else "")
            )
        if isinstance(action, SSHAction):
            return "SSH: " + action.command_id
        return "Telegram: " + action.text

    def _label(self, label: str | None):
        """Name what this thread is doing now, so a group of steps shows all of them."""
        with self.guard:
            if label is None:
                self.active_labels.pop(threading.get_ident(), None)
            else:
                self.active_labels[threading.get_ident()] = label

    def _event(self, kind: str, message: str, rule: str | None = None):
        with self.guard:
            self.event_id += 1
            event = {
                "id": self.event_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "message": message[:2000],
                "rule": rule,
            }
            self.events.appendleft(event)
        logger.info("Sentry %s%s: %s", kind, f" [{rule}]" if rule else "", message)

    def status(self) -> dict:
        with self.guard:
            return {
                "armed": self.armed,
                "test_mode": self.config.test_mode,
                "error": self.error or self.config_error,
                "pending_actions": self.jobs.qsize(),
                "active_action": "; ".join(self.active_labels.values()) or None,
                "rules": [
                    {
                        "name": rule.name,
                        "enabled": rule.enabled,
                        "confirmed": self.states.get(rule.id, RuleState()).latched,
                        "hits": self.states.get(rule.id, RuleState()).hits,
                    }
                    for rule in self.config.rules
                ],
                "events": list(self.events),
                "revision": self.revision,
            }

    def status_v2(self) -> dict:
        """Where Sentry stands, rule by rule, including what the first version cannot say.

        `state` is `disarmed`, `protected` (armed, every rule running), `degraded` (armed,
        some rules paused by a fault) or `fault` (stopped by one).
        """
        now = time.monotonic()
        with self.guard:
            if self.armed:
                state = "degraded" if self.suspended else "protected"
            else:
                state = "fault" if self.error else "disarmed"
            rules = []
            for rule in self.config.rules:
                armed = self.armed and rule.id in self.states
                rule_state = self.states.get(rule.id, RuleState())
                candidate = rule_state.candidate if armed else None
                if not armed:
                    phase = "off"
                elif rule.id in self.suspended:
                    phase = "paused"
                elif candidate is not None:
                    phase = "waiting"
                elif rule_state.latched or rule_state.phase == "active":
                    phase = "latched"
                else:
                    phase = "watching"
                rules.append(
                    {
                        "id": rule.id,
                        "name": rule.name,
                        "enabled": rule.enabled,
                        "trigger": rule.trigger.type,
                        "state": phase,
                        "reason": self.suspended.get(rule.id) if armed else None,
                        "waiting_seconds_left": round(max(0.0, candidate.deadline - now), 1)
                        if candidate is not None
                        else None,
                    }
                )
            return {
                "schema_version": 2,
                "armed": self.armed,
                "state": state,
                "fault_policy": self.config.fault_policy,
                "test_mode": self.config.test_mode,
                "error": self.error or self.config_error,
                "rules": rules,
                "plan": self.plan.as_dict() if self.armed and self.plan else None,
                "revision": self.revision,
            }

    def arm(self) -> dict:
        with self.lifecycle:
            with self.guard:
                if self.armed:
                    return self.status()
                if self.config_error:
                    raise ValueError(self.config_error)
                rules = [r for r in self.config.rules if r.enabled]
                if not rules:
                    raise ValueError("Enable at least one rule before starting Sentry.")
                self._check(rules, self.config.test_mode)
                plan = self.planner.plan(rules)
                if not plan.ok:
                    raise ValueError(
                        "Sentry cannot start: " + "; ".join(str(p) for p in plan.problems)
                    )
            if self.thread is not None:
                self.thread.join(timeout=3)
                if self.thread.is_alive():
                    raise BlockingIOError("Sentry is still stopping; try again shortly.")
            if plan.camera:
                # Only what the rules need: a sensor rule with no camera action starts
                # nothing here, and a photo after a PIR does not load the detector.
                self.video.set_sentry(
                    True, self.config.detection_fps, plan.min_confidence, detect=plan.detector
                )
            try:
                watching = self._watch(plan)
            except Exception:
                if plan.camera:
                    self.video.set_sentry(False)
                raise
            with self.guard:
                self.watching = watching
                self.cancelled = threading.Event()
                self.jobs = queue.Queue(maxsize=16)
                self.states = {rule.id: RuleState() for rule in rules}
                self.suspended = {}
                self.plan = plan
                self.arm_epoch += 1
                self.samples = {}
                self.armed_at = time.monotonic()
                self.error = None
                self.armed = True
                for note in plan.notes:
                    self._event("configured", note)
                self.thread = threading.Thread(target=self._worker, name="sentry-mode-sentry")
                self.thread.start()
                self._event(
                    "armed",
                    "Sentry started in test mode."
                    if self.config.test_mode
                    else "Sentry started with actions enabled.",
                )
                return self.status()

    def _watch(self, plan: Plan) -> dict[str, Lease]:
        """Take a monitoring lease on every other camera a rule watches or photographs."""
        leases: dict[str, Lease] = {}
        owner = f"sentry-{self.arm_epoch + 1}"
        try:
            for source_id, confidence in sorted(plan.vision.items()):
                camera = self.cameras.camera(source_id)
                if camera is None:
                    raise HardwareError(f"{source_id} is no longer available.")
                camera.detection.on_result = partial(self._observed, source_id)
                leases[source_id] = camera.hold(
                    "monitoring",
                    owner,
                    fps=self.config.detection_fps,
                    confidence=confidence,
                    detect=True,
                )
            for source_id in sorted(plan.ready):
                camera = self.cameras.camera(source_id)
                if camera is None:
                    raise HardwareError(f"{source_id} is no longer available.")
                # Streaming before the trigger, so the first photo shows the moment itself.
                leases[source_id] = camera.hold(
                    "monitoring", owner, fps=max(READY_FPS, self.config.detection_fps)
                )
        except Exception:
            for lease in leases.values():
                lease.release()
            raise
        return leases

    def _observed(self, source_id: str, detections: list[Detection], captured: float) -> None:
        self.observe(detections, captured, source_id)

    def _unwatch(self, source_ids=None) -> None:
        with self.guard:
            chosen = list(self.watching) if source_ids is None else list(source_ids)
            leases = [self.watching.pop(s) for s in chosen if s in self.watching]
        for lease in leases:
            lease.release()

    def _stop_cameras(self) -> None:
        self.video.set_sentry(False)
        self._unwatch()

    @staticmethod
    def _groups(
        rule: Rule | RuleV2, created: float, context: ActionContext | None = None
    ) -> list[Job]:
        """Split a rule's steps into the groups the queue runs one after another.

        A step marked with_previous joins the group before it instead of opening a new one.
        Each group's clock starts after the waits that precede it, so a step late in a long
        sequence is not thrown away as stale just because the sequence asked for a pause.
        """
        jobs: list[Job] = []
        delay = 0.0
        actions = resolved(rule) if isinstance(rule, RuleV2) else rule.actions
        for action in actions:
            if jobs and action.with_previous:
                jobs[-1].steps.append(action)
                continue
            if jobs:
                delay += max(
                    (s.seconds for s in jobs[-1].steps if isinstance(s, WaitAction)), default=0.0
                )
            jobs.append(Job(rule.name, [action], created, delay, context))
        return jobs

    def test(self, rule: Rule | RuleV2) -> dict:
        """Run one rule's actions now, because somebody asked for them by pressing a button.

        Test mode holds back what a detection would do, not what the editor was told to do,
        so a test runs its actions for real and needs everything they need.
        """
        with self.lifecycle:
            with self.guard:
                if self.armed:
                    raise BlockingIOError("Disarm Sentry before testing a rule.")
                if isinstance(rule, Rule):
                    rule = rule_to_v2(rule, slug(rule.name))
                self._check([rule])
                problems = self.planner.plan_actions(rule)
                if problems:
                    raise ValueError("; ".join(str(p) for p in problems))
            if self.thread is not None:
                self.thread.join(timeout=3)
                if self.thread.is_alive():
                    raise BlockingIOError("A rule test is still running; try again shortly.")
            with self.guard:
                self.cancelled = threading.Event()
                jobs = self._groups(rule, time.monotonic(), self._context(rule, ("test",)))
                self._event("tested", f"Test run of {rule.name}; running its actions.", rule.name)
                self.thread = threading.Thread(
                    target=self._test, args=(jobs,), name="sentry-mode-test"
                )
                self.thread.start()
            return {"message": "Running the actions on the node."}

    def _test(self, jobs: list[Job]):
        for job in jobs:
            if self.cancelled.is_set():
                break
            self._run(job)
        self._event("tested", "Test run finished.", jobs[0].rule)

    def disarm(self) -> dict:
        with self.lifecycle:
            with self.guard:
                was_armed = self.armed
                self.armed = False
                if was_armed:
                    # Anything still in flight from this arming is now from an old one.
                    self.arm_epoch += 1
                self.cancelled.set()
                self._discard_jobs()
            if self.thread is not None:
                self.thread.join(timeout=6)
                if self.thread.is_alive():
                    raise HardwareError("Sentry actions are still stopping.")
            for recorder in self.recorders:
                recorder.join(timeout=15)
            self.recorders = [r for r in self.recorders if r.is_alive()]
            if self.recorders:
                raise HardwareError(
                    "A Sentry photo, video or audio recording is still being saved."
                )
            self._stop_cameras()
            if was_armed:
                self._event("disarmed", "Sentry stopped; pending actions discarded.")
            return self.status()

    def _discard_jobs(self):
        while True:
            try:
                job = self.jobs.get_nowait()
                self._event("cancelled", "Pending action discarded.", job.rule)
            except queue.Empty:
                return

    def observe(
        self, detections: list[Detection], captured: float, source_id: str = PRIMARY_CAMERA
    ):
        """Detections from one camera, one sample at a time.

        Each camera keeps its own clock of samples. A frame already seen, or older than the
        last one, counts for nothing; a long gap resets only that camera's rules, and a slot
        the scheduler skipped is not taken as an absence.
        """
        with self.guard:
            last = self.samples.get(source_id, 0.0)
            if not self.armed or captured <= max(last, self.armed_at):
                return
            max_gap = max(2.0, 3 / self.config.detection_fps)
            if time.monotonic() - captured > max_gap:
                return
            rules = [
                rule
                for rule in self._watching(VisionTrigger, SequenceTrigger)
                if _looks_at(rule, source_id)
            ]
            if last and captured - last > max_gap:
                for rule in rules:
                    state = self.states[rule.id]
                    state.hits, state.absent_since = 0, None
                    for kind, message in drop(state, f"{source_id} missed frames"):
                        self._event(kind, message, rule.name)
            self.samples[source_id] = captured
            for rule in rules:
                trigger = rule.trigger
                state = self.states[rule.id]
                if isinstance(trigger, SequenceTrigger):
                    candidate = state.candidate
                    count = sum(1 for d in detections if detection_matches(trigger.vision, d))
                    notes = sequence_sample(trigger, state, count, captured, rule.cooldown_seconds)
                    for kind, message in notes:
                        self._event(kind, message, rule.name)
                        if kind == "triggered" and candidate is not None:
                            origin = (candidate.event_id, f"vision:{source_id}")
                            self._fire(rule, captured, origin, candidate.zone)
                    continue
                assert isinstance(trigger, VisionTrigger)
                count = sum(1 for d in detections if detection_matches(trigger, d))
                for kind, message in confirm(rule, state, count, captured):
                    self._event(kind, message, rule.name)
                    if kind == "triggered":
                        self._fire(rule, captured, (f"vision:{trigger.source_id}",), None)

    def observe_event(self, event) -> None:
        """A normalized satellite event, handed over by the satellite service.

        Only events the door marked eligible get here. The event is still checked again
        against this arming: it must be fresh by the hub's own clock, and it must not be
        older than the moment Sentry was armed.
        """
        with self.guard:
            if not self.armed or not getattr(event, "eligible", False):
                return
            now = time.monotonic()
            if event.received_monotonic < self.armed_at:
                return
            if now - event.received_monotonic > self.config.action_ttl_seconds:
                self._event("expired", f"An event from {event.ref.id} arrived too late to act on.")
                return
            if event.kind in MOTION_KINDS and event.value is True and event.quality == "valid":
                self._boost()
            watched = (
                SensorEventTrigger,
                ThresholdTrigger,
                SequenceTrigger,
                AudioEventTrigger,
                PresenceStateTrigger,
            )
            for rule in self._watching(*watched):
                trigger = rule.trigger
                state = self.states[rule.id]
                if isinstance(trigger, AudioEventTrigger):
                    if not sound_fires(trigger, event):
                        continue
                    if self._playing(event.received_monotonic):
                        self._event(
                            "skipped",
                            f"{event.ref.id} heard sound while this hub was playing its own; "
                            "nothing was done.",
                            rule.name,
                        )
                        continue
                    fired = True
                elif isinstance(trigger, SequenceTrigger):
                    camera = self.sources.get(trigger.vision.source_id)
                    zone = camera.zone if camera is not None else None
                    for kind, message in sequence_event(trigger, state, event, zone):
                        self._event(kind, message, rule.name)
                    continue
                elif isinstance(trigger, PresenceStateTrigger):
                    fired = presence_fires(trigger, state, event)
                elif isinstance(trigger, SensorEventTrigger):
                    fired = sensor_fires(trigger, event)
                else:
                    assert isinstance(trigger, ThresholdTrigger)
                    fired = threshold_fires(trigger, state, event)
                if not fired:
                    continue
                if not cooled(state, rule.cooldown_seconds, event.received_monotonic):
                    self._event("skipped", "Still cooling down from the last time.", rule.name)
                    continue
                state.last_trigger = event.received_monotonic
                self._event(
                    "triggered", f"{event.kind} from {event.ref.id}: {event.value!r}.", rule.name
                )
                self._fire(rule, event.received_monotonic, (event.event_id,), event.zone)

    def _playing(self, at: float) -> bool:
        """Whether a microphone may be hearing this hub rather than the house."""
        return self.audio_lock.locked() or at - self.quiet_from < ECHO_SECONDS

    def _boost(self) -> None:
        """A motion sensor fired: look harder, for a while, with every watching camera."""
        plan = self.plan
        if plan is None:
            return
        if plan.detector:
            self.video.detection.boost(BOOST_FACTOR, BOOST_SECONDS)
        for source_id in [s for s in self.watching if s in plan.vision]:
            camera = self.cameras.camera(source_id)
            if camera is not None:
                camera.detection.boost(BOOST_FACTOR, BOOST_SECONDS)

    def _watching(self, *kinds) -> list[RuleV2]:
        """Enabled, unsuspended rules armed in this run whose trigger is one of `kinds`."""
        return [
            rule
            for rule in self.config.rules
            if rule.enabled
            and rule.id in self.states
            and rule.id not in self.suspended
            and isinstance(rule.trigger, kinds)
        ]

    def _context(
        self,
        rule: RuleV2,
        origin: tuple[str, ...],
        zone: str | None = None,
        frames: dict | None = None,
    ) -> ActionContext:
        sources: set[str] = set()
        for action in resolved(rule):
            if isinstance(action, (PhotoAction, VideoAction)):
                sources.add(action.source_id)
            if isinstance(action, VideoAction) and action.microphone:
                sources.add(action.microphone)
            if isinstance(action, AudioAction):
                sources.add(action.audio_source_id)
        return ActionContext.new(
            rule_id=rule.id,
            rule_name=rule.name,
            rule_revision=self.revision,
            arm_epoch=self.arm_epoch,
            zone=zone,
            origin=origin,
            sources=tuple(sorted(sources)),
            frames=frames or {},
        )

    def _trigger_frames(self, rule: RuleV2) -> dict:
        """The newest frame of every camera the rule's first photos use, taken now."""
        frames: dict = {}
        for index, action in enumerate(resolved(rule)):
            if index and not action.with_previous:
                break
            if not isinstance(action, PhotoAction) or action.source_id in frames:
                continue
            camera = self._camera_or_none(action.source_id)
            try:
                got = camera.latest_capture() if camera is not None else None
                if got is not None and time.monotonic() - got[1] <= FRESH_SECONDS:
                    frames[action.source_id] = got
            except Exception:  # noqa: BLE001 - the photo step reports it; the trigger stands
                logger.exception("No frame kept from %s at the trigger", action.source_id)
        return frames

    def _fire(self, rule: RuleV2, created: float, origin: tuple[str, ...], zone) -> None:
        """Queue a rule's whole sequence, or none of it. The caller holds the guard."""
        if self.config.test_mode:
            for action in rule.actions:
                together = "+ " if action.with_previous else ""
                self._event("would_run", together + self._describe(action), rule.name)
            return
        context = self._context(rule, origin, zone, self._trigger_frames(rule))
        jobs = self._groups(rule, created, context)
        free = self.jobs.maxsize - self.jobs.qsize()
        if len(jobs) > free:
            # Half a sequence is worse than none: a greeting without the photo it was
            # meant to accompany, or a wait with nothing after it.
            self._event(
                "skipped",
                f"Action queue is full; the whole sequence of {len(jobs)} steps was skipped.",
                rule.name,
            )
            return
        for job in jobs:
            self.jobs.put_nowait(job)

    def simulate(self, rule: Rule | RuleV2, samples: list[dict]) -> dict:
        """What a rule would do with these observations, on a state of its own.

        Nothing is executed and nothing about the armed engine changes: the rule gets a
        fresh state, the observations are played through the same triggers as a live
        run, and the answer lists what would have been queued at each moment.
        """
        if isinstance(rule, Rule):
            rule = rule_to_v2(rule, slug(rule.name))
        state = RuleState()
        steps = []
        trigger = rule.trigger
        for index, sample in enumerate(samples[:500]):
            at = float(sample.get("at", index))
            fired = False
            notes: list[tuple[str, str]] = []
            if isinstance(trigger, VisionTrigger):
                count = _matching(trigger, sample)
                notes = confirm(rule, state, count, at)
                fired = any(kind == "triggered" for kind, _ in notes)
            elif isinstance(trigger, SequenceTrigger):
                # A sample with detections is the camera; anything else is the sensor.
                if "detections" in sample:
                    count = _matching(trigger.vision, sample)
                    notes = sequence_sample(trigger, state, count, at, rule.cooldown_seconds)
                else:
                    sensor = trigger.sensor
                    observed = Simulated(
                        ref=SourceRef.parse(sample.get("source_id", sensor.source_id)),
                        kind=sample.get("kind", sensor.kind),
                        value=sample.get("value"),
                        quality=sample.get("quality", "valid"),
                        received_monotonic=at,
                        zone=sample.get("zone"),
                        event_id=f"simulated-{index}",
                    )
                    notes = sequence_event(trigger, state, observed, sample.get("camera_zone"))
                fired = any(kind == "triggered" for kind, _ in notes)
            elif isinstance(trigger, (SensorEventTrigger, ThresholdTrigger, AudioEventTrigger)):
                observed = Simulated(
                    ref=SourceRef.parse(sample.get("source_id", trigger.source_id)),
                    kind=sample.get("kind", trigger.kind),
                    value=sample.get("value"),
                    quality=sample.get("quality", "valid"),
                    received_monotonic=at,
                )
                if isinstance(trigger, SensorEventTrigger):
                    hit = sensor_fires(trigger, observed)
                elif isinstance(trigger, AudioEventTrigger):
                    hit = sound_fires(trigger, observed)
                else:
                    hit = threshold_fires(trigger, state, observed)
                if hit and cooled(state, rule.cooldown_seconds, at):
                    state.last_trigger, fired = at, True
                    notes = [("triggered", "The trigger fired.")]
                elif hit:
                    notes = [("skipped", "Still cooling down from the last time.")]
            else:
                raise ValueError(f"{trigger.type} triggers cannot be simulated yet.")
            steps.append(
                {
                    "at": at,
                    "fired": fired,
                    "notes": [{"kind": kind, "message": message} for kind, message in notes],
                    "phase": state.phase if isinstance(trigger, ThresholdTrigger) else None,
                    "would_run": [self._describe(action) for action in rule.actions]
                    if fired
                    else [],
                }
            )
        return {"rule_id": rule.id, "steps": steps, "executed": False}

    def _healthy(self) -> bool:
        """Check what this arming depends on, and act on a failure as the policy says.

        `global` stops everything, as Sentry always did. `isolated` stops only the rules
        that need what failed, and keeps the others armed as long as any are left.
        """
        plan = self.plan
        failures: dict[str, str] = {}
        if plan is None or plan.camera:
            state = self.video.status()
            detection = state["detection"]
            if not state["capture_running"]:
                failures[PRIMARY_CAMERA] = state["error"] or "Camera stopped."
            elif (plan is None or plan.detector) and (
                not detection["enabled"] or detection["error"]
            ):
                failures[DETECTOR] = detection["error"] or "Object detection stopped."
        for source_id in plan.vision if plan else ():
            camera = self.cameras.camera(source_id)
            seen = camera.status() if camera is not None else None
            if seen is None or not seen["capture_running"]:
                failures[source_id] = (seen and seen["error"]) or f"{source_id} stopped."
            elif not seen["detection"]["enabled"] or seen["detection"]["error"]:
                failures.setdefault(
                    DETECTOR, seen["detection"]["error"] or "Object detection stopped."
                )
        for source_id in plan.watched if plan else ():
            record = self.sources.get(source_id)
            if record is None or record.state in (SourceState.DISABLED, SourceState.FAILED):
                failures[source_id] = f"{source_id} is no longer available."
            elif record.ref.node and self.freshness is not None:
                # Silence from a node is not a quiet sensor: its readings are unknown.
                if self.freshness(record.ref.node) == "offline":
                    failures[source_id] = f"{record.ref.node} is offline."
        if not failures:
            return True
        if self.config.fault_policy == "isolated" and plan is not None:
            return self._isolate(plan, failures)
        with self.guard:
            self.error = next(iter(failures.values()))
            self.armed = False
            self.cancelled.set()
            self._discard_jobs()
            self._event("fault", self.error)
        self._stop_cameras()
        return False

    def _isolate(self, plan: Plan, failures: dict[str, str]) -> bool:
        """Pause the rules that need what failed; keep the rest armed if any are left.

        A camera that fails pauses the rules that use it. The detector is shared, so when
        it fails every rule that looks for objects pauses, and the cameras stay on only
        for the photos and videos the remaining rules take.
        """
        with self.guard:
            for source_id, message in failures.items():
                for rule_id in plan.rules_needing(source_id) - set(self.suspended):
                    self.suspended[rule_id] = message
                    rule = next(r for r in self.config.rules if r.id == rule_id)
                    self._event("fault", f"Rule paused: {message}", rule.name)
                    drop(self.states[rule_id], "the rule was paused")
            left = set(self.states) - set(self.suspended)
            if left:
                needed = set().union(*(plan.needs.get(rule_id, ()) for rule_id in left))
                if DETECTOR in failures:
                    lost = {}
                    kept = {s for s in plan.vision if s in needed and s not in failures}
                else:
                    lost = {s: c for s, c in plan.vision.items() if s not in failures}
                    kept = set()
                self.plan = replace(plan, vision=lost, ready=plan.ready | kept)
                primary = PRIMARY_CAMERA in needed and PRIMARY_CAMERA not in failures
                if plan.camera and PRIMARY_CAMERA in failures:
                    # Nothing left may use the camera; let it go rather than retry it.
                    self.plan = replace(self.plan, detector=False, camera=False)
                    self.video.set_sentry(False)
                elif plan.detector and DETECTOR in failures:
                    self.plan = replace(self.plan, detector=False, camera=primary)
                    self.video.set_sentry(False)
                    if primary:
                        self._keep_camera()
                self._unwatch((set(failures) | set(plan.vision)) - set(lost) - kept)
                return True
            self.error = "; ".join(failures.values())
            self.armed = False
            self.cancelled.set()
            self._discard_jobs()
            self._event("fault", "Every armed rule depends on something that failed.")
        self._stop_cameras()
        return False

    def _keep_camera(self) -> None:
        """This node's camera again, without the detector, for the rules still armed."""
        try:
            self.video.set_sentry(True, self.config.detection_fps, detect=False)
        except Exception as exc:  # noqa: BLE001 - the next health check pauses what needs it
            logger.warning("The camera could not restart without the detector: %s", exc)

    def _expire(self) -> None:
        """Close the sequence windows that ran out with nothing arriving to notice."""
        now = time.monotonic()
        with self.guard:
            for rule in self._watching(SequenceTrigger):
                assert isinstance(rule.trigger, SequenceTrigger)
                for kind, message in expire(rule.trigger, self.states[rule.id], now):
                    self._event(kind, message, rule.name)

    def _worker(self):
        while not self.cancelled.is_set():
            if not self._healthy():
                break
            self._expire()
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            self._run(job)
        if self.error:
            self._stop_cameras()

    def _run(self, job: Job):
        """Execute one group of a rule's sequence, whether an appearance or a test queued it.

        The steps of a group start together and the group is only done when all of them
        are; a step that fails says so in the log and the sequence carries on.
        """
        deadline = job.created + job.delay + self.config.action_ttl_seconds
        if self.cancelled.is_set():
            return
        if job.context is not None and job.context.arm_epoch != self.arm_epoch:
            self._event("cancelled", "Action belonged to an earlier arming.", job.rule)
            return
        if job.context is not None and job.context.trigger_id in self.halted:
            self._event("cancelled", "An earlier step stopped this sequence.", job.rule)
            return
        if len(job.steps) == 1:
            self._step(job.rule, job.steps[0], deadline, job.context)
            return
        threads = [
            threading.Thread(
                target=self._step,
                args=(job.rule, action, deadline, job.context),
                name="sentry-mode-step",
            )
            for action in job.steps
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def _step(
        self, rule: str, action: Action, deadline: float, context: ActionContext | None = None
    ):
        """Execute one action of the sequence."""
        try:
            if self.cancelled.is_set():
                return
            if isinstance(action, WaitAction):
                # The deadlines of later groups already allow for this pause, so a wait is
                # never dropped as stale; it only ends early when Sentry is disarmed.
                self._event("waiting", f"Waiting {action.seconds:g} s.", rule)
                self.cancelled.wait(action.seconds)
                return
            if time.monotonic() > deadline:
                self._event("expired", "Action became stale before it could start.", rule)
                return
            finished = "Action completed."
            if isinstance(action, PhotoAction) and action.count > 1:
                self._start_photo_series(rule, action, context)
                return
            if isinstance(action, PhotoAction):
                self._label("Photo: " + rule)
                finished = "Photo saved: " + self._save_photo(rule, action, context)
            elif isinstance(action, VideoAction):
                self._start_recording(rule, action, context)
                return
            elif isinstance(action, AudioAction):
                self._start_audio(rule, action, context)
                return
            elif isinstance(action, (TTSAction, TuneAction, SoundAction)):
                kind = {TTSAction: "announcement", TuneAction: "tune"}.get(
                    type(action), "audio file"
                )
                while not self.cancelled.is_set() and time.monotonic() < deadline:
                    if self.audio_lock.acquire(timeout=0.1):
                        break
                else:
                    self._event("expired", f"Speaker stayed busy; {kind} skipped.", rule)
                    return
                try:
                    if self.cancelled.is_set():
                        return
                    if time.monotonic() > deadline:
                        self._event(
                            "expired",
                            f"{kind.capitalize()} became stale while waiting for the speaker.",
                            rule,
                        )
                        return
                    if isinstance(action, TTSAction):
                        self._label("TTS: " + rule)
                        self._event("action_started", "Playing announcement.", rule)
                        speak(
                            self.settings,
                            action.text,
                            action.voice,
                            action.rate,
                            effects=action.effects,
                            stop_event=self.cancelled,
                        )
                    elif isinstance(action, SoundAction):
                        self._label("Audio file: " + rule)
                        self._event(
                            "action_started",
                            "Playing audio file: " + action.sound,
                            rule,
                        )
                        self.sounds.play(
                            self.settings,
                            action.sound,
                            repeat=action.repeat,
                            volume=action.volume,
                            stop_event=self.cancelled,
                        )
                    else:
                        self._label("Tune: " + rule)
                        self._event(
                            "action_started",
                            "Playing tune: " + TUNES[action.tune][0],
                            rule,
                        )
                        self._play_tune(action)
                finally:
                    self.quiet_from = time.monotonic()
                    self.audio_lock.release()
            elif isinstance(action, SSHAction):
                self._label("SSH: " + action.command_id)
                self._event(
                    "action_started",
                    "Running saved SSH command: " + action.command_id,
                    rule,
                )
                self._ssh(self.config.ssh_commands[action.command_id])
            else:
                self._label("Telegram: " + rule)
                self._event("action_started", "Sending Telegram message.", rule)
                self._telegram(action)
            self._event("action_finished", finished, rule)
        except Unavailable as exc:
            self._unavailable(rule, action, context, str(exc))
        except Exception as exc:
            self._event("cancelled" if self.cancelled.is_set() else "action_failed", str(exc), rule)
        finally:
            self._label(None)

    def _unavailable(
        self, rule: str, action: Action, context: ActionContext | None, reason: str
    ) -> None:
        """A camera had nothing to give: do what the step said, and never use another one."""
        if self.cancelled.is_set():
            self._event("cancelled", reason, rule)
            return
        policy = getattr(action, "if_unavailable", "fail")
        if policy == "skip":
            self._event("skipped", f"{reason}; step skipped.", rule)
            return
        if policy == "stop":
            if context is not None:
                with self.guard:
                    self.halted.append(context.trigger_id)
            self._event("cancelled", f"{reason}; the rest of this sequence was dropped.", rule)
            return
        self._event("action_failed", f"{reason}; no other camera was used.", rule)

    def _play_tune(self, action: TuneAction):
        play_tune(
            self.settings.speaker,
            action.tune,
            action.repeat,
            action.volume,
            action.pitch,
            stop_event=self.cancelled,
        )

    def _camera_or_none(self, source_id: str):
        if source_id == PRIMARY_CAMERA:
            return self.video
        return self.cameras.camera(source_id)

    def _camera(self, source_id: str):
        camera = self._camera_or_none(source_id)
        if camera is None:
            raise Unavailable(f"{source_id} is not a camera on this hub")
        return camera

    def _recent(self, source_id: str) -> tuple:
        """A frame from this camera no older than `FRESH_SECONDS`, waiting briefly for one."""
        camera = self._camera(source_id)
        deadline = time.monotonic() + FRAME_WAIT_SECONDS
        while True:
            got = camera.latest_capture()
            now = time.monotonic()
            if got is not None and now - got[1] <= FRESH_SECONDS:
                return got
            if now >= deadline or self.cancelled.wait(0.05):
                state = (
                    "has stopped" if got is None else f"last sent a frame {now - got[1]:.0f} s ago"
                )
                raise Unavailable(f"{source_id} {state}")

    def _evidence(self, context: ActionContext | None, source_id: str | None, **details) -> dict:
        """What a sidecar says about where a capture came from and why it was taken."""
        record = self.sources.get(source_id) if source_id else None
        document: dict[str, object] = {
            "source_id": source_id,
            "source_name": record.display_name if record else source_id,
            "origin": record.origin if record else None,
            "zone": record.zone if record else None,
        }
        if context is not None:
            document.update(
                rule_id=context.rule_id,
                rule_name=context.rule_name,
                rule_revision=context.rule_revision,
                trigger_id=context.trigger_id,
                trigger_origin=list(context.origin),
                trigger_zone=context.zone,
                arm_epoch=context.arm_epoch,
                triggered_at=_wall(context.decided_wall),
            )
        document.update(details)
        return document

    def _save_photo(
        self,
        rule: str,
        action: PhotoAction | None = None,
        context: ActionContext | None = None,
        index: int = 0,
    ) -> str:
        """One photo from the action's camera: the frame kept at the trigger, or a new one."""
        source_id = action.source_id if action is not None else PRIMARY_CAMERA
        kept = context.frames.get(source_id) if context is not None and index == 0 else None
        if kept is not None:
            (frame, captured), timing = kept, "at_trigger"
        else:
            (frame, captured), timing = self._recent(source_id), "after_trigger"
        now = time.monotonic()
        details = {
            "timing": timing,
            "captured_at": _wall(time.time() - (now - captured)),
            "frame_age_seconds": round(
                (context.decided_at if kept is not None and context else now) - captured, 3
            ),
            "sequence": {"index": index + 1, "count": action.count if action else 1},
        }
        if context is not None:
            details["seconds_after_trigger"] = round(captured - context.decided_at, 3)
        name = self.captures.save_photo(frame, rule, self._evidence(context, source_id, **details))
        return name if source_id == PRIMARY_CAMERA else f"{name} from {source_id}"

    def _in_background(self, rule: str, started: str, work):
        # Videos and photo series run beside the queue, so an announcement is not held back.
        self.recorders = [r for r in self.recorders if r.is_alive()]
        recorder = threading.Thread(target=work, name="sentry-mode-recording")
        self.recorders.append(recorder)
        self._event("action_started", started, rule)
        recorder.start()

    def _start_photo_series(
        self, rule: str, action: PhotoAction, context: ActionContext | None = None
    ):
        cancelled = self.cancelled
        # The first photo decides, as a single one would, whether the sequence goes on.
        first = self._save_photo(rule, action, context, 0)

        def take():
            start = time.monotonic()
            for index in range(1, action.count):
                if cancelled.wait(
                    max(0, start + index * action.interval_seconds - time.monotonic())
                ):
                    self._event("cancelled", f"Photo series stopped after {index}.", rule)
                    return
                try:
                    name = self._save_photo(rule, action, context, index)
                    self._event("action_finished", "Photo saved: " + name, rule)
                except Exception as exc:
                    self._event("action_failed", f"Photo {index + 1} failed: {exc}", rule)

        self._in_background(
            rule, f"Taking {action.count} photos every {action.interval_seconds:g} s.", take
        )
        self._event("action_finished", "Photo saved: " + first, rule)

    def _microphone(self) -> list[str]:
        return Microphone(self.settings.microphone).ffmpeg_input()

    def _start_recording(
        self, rule: str, action: VideoAction, context: ActionContext | None = None
    ):
        cancelled, seconds = self.cancelled, action.duration_seconds
        source_id = action.source_id
        camera = self._camera(source_id)
        self._recent(source_id)
        if source_id == PRIMARY_CAMERA:
            lease = self.video.hold_recording(f"rule:{rule}")
            size: tuple[int, int] | None = self.video.recording_size()
        else:
            lease = camera.hold("recording", f"rule:{rule}")
            size = None
        microphone_id = action.microphone

        def frames():
            got = camera.latest_capture()
            if got is None or time.monotonic() - got[1] > FRESH_SECONDS:
                return None
            return got[0]

        def record():
            try:
                with ExitStack() as stack:
                    microphone: SoundSource | None = None
                    sound_error = None
                    alignment: dict[str, str] = {}
                    if microphone_id == PRIMARY_MICROPHONE:
                        try:
                            microphone = self._microphone()
                        except HardwareError as exc:
                            sound_error = str(exc)
                    elif microphone_id is not None:
                        remote = self.microphones.get(microphone_id)
                        if remote is None:
                            sound_error = f"{microphone_id} cannot be recorded by this hub"
                        else:
                            microphone = stack.enter_context(remote.recording(f"rule:{rule}"))
                            alignment = {"sound_alignment": "hub_arrival"}
                    elif action.audio:
                        sound_error = f"no microphone is chosen for {source_id}"
                    started = time.monotonic()
                    name, audio_error = self.captures.record_video(
                        frames,
                        seconds,
                        rule,
                        cancelled,
                        size=size,
                        microphone=microphone,
                        meta=self._evidence(
                            context,
                            source_id,
                            timing="after_trigger",
                            audio_source_id=microphone_id,
                            seconds_after_trigger=None
                            if context is None
                            else round(started - context.decided_at, 3),
                            **alignment,
                        ),
                    )
                sound_error = sound_error or audio_error
                where = "" if source_id == PRIMARY_CAMERA else f" from {source_id}"
                self._event(
                    "action_finished",
                    "Video saved: "
                    + name
                    + where
                    + (f" (no sound: {sound_error})" if sound_error else ""),
                    rule,
                )
            except Exception as exc:
                self._event("action_failed", f"Video recording failed: {exc}", rule)
            finally:
                lease.release()

        sound = " with sound" if microphone_id else ""
        where = "" if source_id == PRIMARY_CAMERA else f" from {source_id}"
        self._in_background(rule, f"Recording a {seconds}-second video{where}{sound}.", record)

    def _start_audio(
        self, rule: str, action: AudioAction | int, context: ActionContext | None = None
    ):
        if isinstance(action, int):
            action = AudioAction(duration_seconds=action)
        cancelled, seconds = self.cancelled, action.duration_seconds
        microphone_id = action.audio_source_id
        remote = self.microphones.get(microphone_id)
        if microphone_id != PRIMARY_MICROPHONE and remote is None:
            raise Unavailable(f"{microphone_id} cannot be recorded by this hub")

        def record():
            try:
                meta = self._evidence(context, microphone_id, timing="after_trigger")
                if remote is None:
                    name = self.captures.record_audio(
                        self._microphone(), seconds, rule, cancelled, meta=meta
                    )
                else:
                    meta["sound_alignment"] = "hub_arrival"
                    with remote.recording(f"rule:{rule}") as sound:
                        name = self.captures.record_audio(
                            sound, seconds, rule, cancelled, meta=meta
                        )
                self._event("action_finished", "Audio saved: " + name, rule)
            except Exception as exc:
                self._event("action_failed", f"Audio recording failed: {exc}", rule)

        self._in_background(rule, f"Recording {seconds} seconds of audio.", record)

    def _telegram(self, action: TelegramAction):
        # Isolate DNS/network I/O so disarm and the absolute timeout can reap it promptly.
        # Secrets travel through stdin, never process arguments or error URLs.
        payload = json.dumps(
            {
                "token": self.config.telegram.bot_token.get_secret_value(),
                "chat_id": self.config.telegram.chat_id,
                "text": action.text,
                "disable_notification": action.silent,
            }
        ).encode()
        if self.cancelled.is_set():
            raise HardwareError("Telegram action cancelled.")
        process = subprocess.Popen(
            [sys.executable, "-m", "sentry_mode.sentry.telegram"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        first = True
        try:
            while True:
                if self.cancelled.is_set():
                    raise HardwareError(
                        "Telegram action cancelled; delivery may already have occurred."
                    )
                if time.monotonic() >= deadline:
                    raise HardwareError(
                        "Telegram timed out; delivery is unknown. No retry was made."
                    )
                try:
                    output, _ = process.communicate(input=payload if first else None, timeout=0.1)
                except subprocess.TimeoutExpired:
                    first = False
                    continue
                if process.returncode:
                    # Child emits only fixed messages, never provider response bodies or URLs.
                    raise HardwareError(output.decode(errors="replace")[:500] or "Telegram failed.")
                return
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def _ssh(self, command: SSHCommand):
        args = [
            "ssh",
            "-n",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "PermitLocalCommand=no",
            "-p",
            str(command.port),
        ]
        if command.identity_file:
            args += ["-i", str(Path(command.identity_file).expanduser())]
        target = f"{command.user}@{command.host}" if command.user else command.host
        args += ["--", target, command.command]
        process = None
        with tempfile.TemporaryFile() as errors:
            try:
                if self.cancelled.is_set():
                    raise HardwareError("SSH action cancelled.")
                process = subprocess.Popen(
                    args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
                )
                deadline = time.monotonic() + command.timeout_seconds
                while process.poll() is None:
                    if self.cancelled.wait(0.1):
                        raise HardwareError(
                            "SSH action cancelled. Remote commands may already have started."
                        )
                    if time.monotonic() >= deadline:
                        raise HardwareError("SSH command timed out; it will not be retried.")
                if process.returncode:
                    errors.seek(0)
                    detail = errors.read(1500).decode(errors="replace")
                    raise HardwareError(f"SSH exited with {process.returncode}: {detail}")
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def _looks_at(rule: RuleV2, source_id: str) -> bool:
    trigger = rule.trigger
    if isinstance(trigger, SequenceTrigger):
        return trigger.vision.source_id == source_id
    return isinstance(trigger, VisionTrigger) and trigger.source_id == source_id


def _wall(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


def confirm(rule: RuleV2, state: RuleState, count: int, captured: float) -> list[tuple[str, str]]:
    """One camera sample through a vision rule. Returns the events it produced.

    The rule fires after enough consecutive samples with enough matching objects, stays
    latched while they remain, and becomes ready again only after a real absence.
    """
    trigger = rule.trigger
    assert isinstance(trigger, VisionTrigger)
    notes: list[tuple[str, str]] = []
    if count < trigger.min_count:
        state.hits = 0
        if state.absent_since is None:
            state.absent_since = captured
        if state.latched and captured - state.absent_since >= trigger.rearm_after_absence_seconds:
            state.latched = False
            notes.append(("rearmed", "Object absent long enough; ready for another appearance."))
        return notes
    state.absent_since = None
    state.hits = min(trigger.consecutive_detections, state.hits + 1)
    if state.hits == 1 and not state.latched:
        notes.append(("detected", f"{trigger.object}: {count} matching object(s)."))
    if state.latched or state.hits < trigger.consecutive_detections:
        return notes
    if not cooled(state, rule.cooldown_seconds, captured):
        return notes
    state.latched, state.last_trigger = True, captured
    notes.append(("triggered", f"Confirmed {trigger.object} appearance."))
    return notes


def _matching(trigger: VisionTrigger, sample: dict) -> int:
    detections = [
        Detection(d["label"], float(d["confidence"]), tuple(d["box"]))
        for d in sample.get("detections", [])
    ]
    return sum(1 for d in detections if detection_matches(trigger, d))


@dataclass(frozen=True)
class Simulated:
    """An observation made up for a simulation, shaped like a normalized event."""

    ref: SourceRef
    kind: str
    value: object
    quality: str
    received_monotonic: float
    zone: str | None = None
    event_id: str = "simulated"

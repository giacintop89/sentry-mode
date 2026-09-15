"""Confirmed appearances, a bounded action queue, and explicit arm/disarm lifecycle."""

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sentry_node.audio.sounds import SoundLibrary
from sentry_node.audio.speech import speak
from sentry_node.audio.tunes import TUNES, play_tune
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.microphone import Microphone
from sentry_node.sentry.config import (
    AudioAction,
    PhotoAction,
    Rule,
    SentryConfig,
    SoundAction,
    SSHAction,
    SSHCommand,
    TelegramAction,
    TTSAction,
    TuneAction,
    VideoAction,
)
from sentry_node.vision.detection import Detection
from sentry_node.vision.labels import CLASSES
from sentry_node.vision.recording import Captures
from sentry_node.vision.stream import VideoStream

logger = logging.getLogger(__name__)


@dataclass
class RuleState:
    hits: int = 0
    latched: bool = False
    absent_since: float | None = None
    last_trigger: float = float("-inf")


@dataclass
class Job:
    rule: str
    action: (
        PhotoAction
        | AudioAction
        | VideoAction
        | TTSAction
        | TuneAction
        | SoundAction
        | SSHAction
        | TelegramAction
    )
    created: float


class Sentry:
    def __init__(self, settings: Settings, video: VideoStream, audio_lock):
        self.settings, self.video, self.audio_lock = settings, video, audio_lock
        self.guard = threading.RLock()
        self.lifecycle = threading.Lock()
        self.config = settings.sentry.model_copy(deep=True)
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
        self.last_sample = 0.0
        self.active_action: str | None = None
        self.captures = Captures(settings.captures_directory)
        self.sounds = SoundLibrary(settings.sounds_directory)
        self.recorders: list[threading.Thread] = []
        self._load()
        self.video.detection.on_result = self.observe
        self.video.detection.on_error = self.fault
        self.video.on_error = self.fault

    def fault(self, message: str):
        with self.guard:
            if not self.armed:
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
            data = json.loads(path.read_text())
            self.config = SentryConfig.model_validate(data["config"])
            self.revision = int(data["revision"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.config_error = f"Saved Sentry configuration could not be loaded: {exc}"

    def configuration(self) -> dict:
        with self.guard:
            config = self.config.model_dump(mode="json")
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

    def update(
        self, config: SentryConfig, revision: int, clear_telegram_token: bool = False
    ) -> dict:
        with self.lifecycle, self.guard:
            if self.armed:
                raise BlockingIOError("Disarm Sentry before changing rules or actions.")
            if revision != self.revision:
                raise BlockingIOError("Configuration changed in another tab. Reload before saving.")
            config = config.model_copy(deep=True)
            if clear_telegram_token:
                from pydantic import SecretStr

                config.telegram.bot_token = SecretStr("")
            elif not config.telegram.bot_token.get_secret_value():
                config.telegram.bot_token = self.config.telegram.bot_token
            saved_config = config.model_dump(mode="json")
            saved_config["telegram"]["bot_token"] = config.telegram.bot_token.get_secret_value()
            path = self.settings.sentry_state_file
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = None
            try:
                with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as out:
                    name = Path(out.name)
                    json.dump({"config": saved_config, "revision": revision + 1}, out)
                    out.flush()
                    os.fsync(out.fileno())
                name.replace(path)
            finally:
                if name is not None:
                    name.unlink(missing_ok=True)
            self.config, self.revision, self.config_error = config, revision + 1, None
            self._event("configured", "Rules and actions saved.")
            return self.configuration()

    def _check(self, rules: list[Rule], logged_only: bool = False):
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
        if isinstance(action, PhotoAction):
            return (
                f"Photos: {action.count} every {action.interval_seconds:g} s"
                if action.count > 1
                else "Photo"
            )
        if isinstance(action, VideoAction):
            return f"Video: {action.duration_seconds} s" + (" with sound" if action.audio else "")
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
                "active_action": self.active_action,
                "rules": [
                    {
                        "name": rule.name,
                        "enabled": rule.enabled,
                        "confirmed": self.states.get(rule.name, RuleState()).latched,
                        "hits": self.states.get(rule.name, RuleState()).hits,
                    }
                    for rule in self.config.rules
                ],
                "events": list(self.events),
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
            if self.thread is not None:
                self.thread.join(timeout=3)
                if self.thread.is_alive():
                    raise BlockingIOError("Sentry is still stopping; try again shortly.")
            self.video.set_sentry(
                True, self.config.detection_fps, min(rule.min_confidence for rule in rules)
            )
            with self.guard:
                self.cancelled = threading.Event()
                self.jobs = queue.Queue(maxsize=16)
                self.states = {rule.name: RuleState() for rule in rules}
                self.last_sample = 0
                self.armed_at = time.monotonic()
                self.error = None
                self.armed = True
                self.thread = threading.Thread(target=self._worker, name="sentry-node-sentry")
                self.thread.start()
                self._event(
                    "armed",
                    "Sentry started in test mode."
                    if self.config.test_mode
                    else "Sentry started with actions enabled.",
                )
                return self.status()

    def test(self, rule: Rule) -> dict:
        """Run one rule's actions now, because somebody asked for them by pressing a button.

        Test mode holds back what a detection would do, not what the editor was told to do,
        so a test runs its actions for real and needs everything they need.
        """
        with self.lifecycle:
            with self.guard:
                if self.armed:
                    raise BlockingIOError("Disarm Sentry before testing a rule.")
                self._check([rule])
            if self.thread is not None:
                self.thread.join(timeout=3)
                if self.thread.is_alive():
                    raise BlockingIOError("A rule test is still running; try again shortly.")
            with self.guard:
                self.cancelled = threading.Event()
                created = time.monotonic()
                jobs = [Job(rule.name, action, created) for action in rule.actions]
                self._event("tested", f"Test run of {rule.name}; running its actions.", rule.name)
                self.thread = threading.Thread(
                    target=self._test, args=(jobs,), name="sentry-node-test"
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
            self.video.set_sentry(False)
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

    def observe(self, detections: list[Detection], captured: float):
        with self.guard:
            if not self.armed or captured <= max(self.last_sample, self.armed_at):
                return
            max_gap = max(2.0, 3 / self.config.detection_fps)
            if time.monotonic() - captured > max_gap:
                return
            if self.last_sample and captured - self.last_sample > max_gap:
                for state in self.states.values():
                    state.hits, state.absent_since = 0, None
            self.last_sample = captured
            for rule in self.config.rules:
                if not rule.enabled:
                    continue
                state = self.states[rule.name]
                matches = [d for d in detections if self._matches(rule, d)]
                if len(matches) < rule.min_count:
                    state.hits = 0
                    if state.absent_since is None:
                        state.absent_since = captured
                    if (
                        state.latched
                        and captured - state.absent_since >= rule.rearm_after_absence_seconds
                    ):
                        state.latched = False
                        self._event(
                            "rearmed",
                            "Object absent long enough; ready for another appearance.",
                            rule.name,
                        )
                    continue
                state.absent_since = None
                state.hits = min(rule.consecutive_detections, state.hits + 1)
                if state.hits == 1 and not state.latched:
                    self._event(
                        "detected", f"{rule.object}: {len(matches)} matching object(s).", rule.name
                    )
                if state.latched or state.hits < rule.consecutive_detections:
                    continue
                if captured - state.last_trigger < rule.cooldown_seconds:
                    continue
                state.latched, state.last_trigger = True, captured
                self._event("triggered", f"Confirmed {rule.object} appearance.", rule.name)
                for action in rule.actions:
                    if self.config.test_mode:
                        self._event("would_run", self._describe(action), rule.name)
                        continue
                    try:
                        self.jobs.put_nowait(Job(rule.name, action, captured))
                    except queue.Full:
                        self._event("skipped", "Action queue is full.", rule.name)

    @staticmethod
    def _matches(rule: Rule, detection: Detection) -> bool:
        if detection.label != rule.object or detection.confidence < rule.min_confidence:
            return False
        if rule.region is None:
            return True
        x1, y1, x2, y2 = detection.box
        left, top, right, bottom = rule.region
        return left <= (x1 + x2) / 2 <= right and top <= (y1 + y2) / 2 <= bottom

    def _healthy(self) -> bool:
        state = self.video.status()
        detection = state["detection"]
        if state["capture_running"] and detection["enabled"] and not detection["error"]:
            return True
        with self.guard:
            self.error = state["error"] or detection["error"] or "Camera or detector stopped."
            self.armed = False
            self.cancelled.set()
            self._discard_jobs()
            self._event("fault", self.error)
        self.video.set_sentry(False)
        return False

    def _worker(self):
        while not self.cancelled.is_set():
            if not self._healthy():
                break
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            self._run(job)
        if self.error:
            self.video.set_sentry(False)

    def _run(self, job: Job):
        """Execute one action, the same way whether an appearance or a test queued it."""
        deadline = job.created + self.config.action_ttl_seconds
        try:
            if self.cancelled.is_set():
                return
            if time.monotonic() > deadline:
                self._event("expired", "Action became stale before it could start.", job.rule)
                return
            finished = "Action completed."
            if isinstance(job.action, PhotoAction) and job.action.count > 1:
                self._start_photo_series(job.rule, job.action)
                return
            if isinstance(job.action, PhotoAction):
                self.active_action = "Photo: " + job.rule
                finished = "Photo saved: " + self._save_photo(job.rule)
            elif isinstance(job.action, VideoAction):
                self._start_recording(job.rule, job.action)
                return
            elif isinstance(job.action, AudioAction):
                self._start_audio(job.rule, job.action.duration_seconds)
                return
            elif isinstance(job.action, (TTSAction, TuneAction, SoundAction)):
                kind = {TTSAction: "announcement", TuneAction: "tune"}.get(
                    type(job.action), "audio file"
                )
                while not self.cancelled.is_set() and time.monotonic() < deadline:
                    if self.audio_lock.acquire(timeout=0.1):
                        break
                else:
                    self._event("expired", f"Speaker stayed busy; {kind} skipped.", job.rule)
                    return
                try:
                    if self.cancelled.is_set():
                        return
                    if time.monotonic() > deadline:
                        self._event(
                            "expired",
                            f"{kind.capitalize()} became stale while waiting for the speaker.",
                            job.rule,
                        )
                        return
                    if isinstance(job.action, TTSAction):
                        self.active_action = "TTS: " + job.rule
                        self._event("action_started", "Playing announcement.", job.rule)
                        speak(
                            self.settings,
                            job.action.text,
                            job.action.voice,
                            job.action.rate,
                            effects=job.action.effects,
                            stop_event=self.cancelled,
                        )
                    elif isinstance(job.action, SoundAction):
                        self.active_action = "Audio file: " + job.rule
                        self._event(
                            "action_started",
                            "Playing audio file: " + job.action.sound,
                            job.rule,
                        )
                        self.sounds.play(
                            self.settings,
                            job.action.sound,
                            repeat=job.action.repeat,
                            volume=job.action.volume,
                            stop_event=self.cancelled,
                        )
                    else:
                        self.active_action = "Tune: " + job.rule
                        self._event(
                            "action_started",
                            "Playing tune: " + TUNES[job.action.tune][0],
                            job.rule,
                        )
                        self._play_tune(job.action)
                finally:
                    self.audio_lock.release()
            elif isinstance(job.action, SSHAction):
                self.active_action = "SSH: " + job.action.command_id
                self._event(
                    "action_started",
                    "Running saved SSH command: " + job.action.command_id,
                    job.rule,
                )
                self._ssh(self.config.ssh_commands[job.action.command_id])
            else:
                self.active_action = "Telegram: " + job.rule
                self._event("action_started", "Sending Telegram message.", job.rule)
                self._telegram(job.action)
            self._event("action_finished", finished, job.rule)
        except Exception as exc:
            self._event(
                "cancelled" if self.cancelled.is_set() else "action_failed", str(exc), job.rule
            )
        finally:
            self.active_action = None

    def _play_tune(self, action: TuneAction):
        play_tune(
            self.settings.speaker,
            action.tune,
            action.repeat,
            action.volume,
            action.pitch,
            stop_event=self.cancelled,
        )

    def _save_photo(self, rule: str) -> str:
        frame = self.video.latest_frame()
        if frame is None:
            raise HardwareError("No camera frame is available for a photo.")
        return self.captures.save_photo(frame, rule)

    def _in_background(self, rule: str, started: str, work):
        # Videos and photo series run beside the queue, so an announcement is not held back.
        self.recorders = [r for r in self.recorders if r.is_alive()]
        recorder = threading.Thread(target=work, name="sentry-node-recording")
        self.recorders.append(recorder)
        self._event("action_started", started, rule)
        recorder.start()

    def _start_photo_series(self, rule: str, action: PhotoAction):
        cancelled = self.cancelled

        def take():
            start = time.monotonic()
            for index in range(action.count):
                if index and cancelled.wait(
                    max(0, start + index * action.interval_seconds - time.monotonic())
                ):
                    self._event("cancelled", f"Photo series stopped after {index}.", rule)
                    return
                try:
                    self._event("action_finished", "Photo saved: " + self._save_photo(rule), rule)
                except Exception as exc:
                    self._event("action_failed", f"Photo {index + 1} failed: {exc}", rule)

        self._in_background(
            rule, f"Taking {action.count} photos every {action.interval_seconds:g} s.", take
        )

    def _microphone(self) -> list[str]:
        return Microphone(self.settings.microphone).ffmpeg_input()

    def _start_recording(self, rule: str, action: VideoAction):
        cancelled, seconds = self.cancelled, action.duration_seconds
        self.video.add_recording(1)

        def record():
            try:
                microphone = sound_error = None
                if action.audio:
                    try:
                        microphone = self._microphone()
                    except HardwareError as exc:
                        sound_error = str(exc)
                name, audio_error = self.captures.record_video(
                    self.video.latest_frame,
                    seconds,
                    rule,
                    cancelled,
                    size=self.video.recording_size(),
                    microphone=microphone,
                )
                sound_error = sound_error or audio_error
                self._event(
                    "action_finished",
                    "Video saved: " + name + (f" (no sound: {sound_error})" if sound_error else ""),
                    rule,
                )
            except Exception as exc:
                self._event("action_failed", f"Video recording failed: {exc}", rule)
            finally:
                self.video.add_recording(-1)

        sound = " with sound" if action.audio else ""
        self._in_background(rule, f"Recording a {seconds}-second video{sound}.", record)

    def _start_audio(self, rule: str, seconds: int):
        cancelled = self.cancelled

        def record():
            try:
                name = self.captures.record_audio(self._microphone(), seconds, rule, cancelled)
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
            [sys.executable, "-m", "sentry_node.sentry.telegram"],
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

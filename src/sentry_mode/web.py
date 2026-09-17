"""HTTP controls using the same configuration and hardware adapters as the CLI."""

import itertools
import json
import logging
import re
import signal
import ssl
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from sentry_mode.app import run
from sentry_mode.audio.effects import VoiceEffects
from sentry_mode.audio.monitor import RATE as AUDIO_RATE
from sentry_mode.audio.monitor import AudioMonitor
from sentry_mode.audio.soundboard import Soundboard, SoundboardMessage
from sentry_mode.audio.sounds import MAX_UPLOAD_BYTES
from sentry_mode.audio.speech import (
    available_voices,
    play_sample,
    render_sample,
    sample_audio,
    speak,
)
from sentry_mode.audio.talk import MAX_CHUNK, TalkStream
from sentry_mode.audio.tunes import TUNES, play_tune
from sentry_mode.config import (
    AudioConfig,
    CameraConfig,
    Settings,
    SpeakerConfig,
    config_path,
    save_sections,
)
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.camera import Camera, list_cameras
from sentry_mode.hardware.microphone import Microphone
from sentry_mode.hardware.speaker import Speaker
from sentry_mode.hardware.status import inspect_hardware, network_available
from sentry_mode.sentry.config import Rule, RuleV2, SentryConfig, SentryConfigV2, TuneAction
from sentry_mode.sentry.engine import Sentry
from sentry_mode.sentry.migration import SchemaUpgradeRequired
from sentry_mode.sources.legacy import legacy_sources
from sentry_mode.sources.manager import SourceManager
from sentry_mode.sources.models import PRIMARY_CAMERA, PRIMARY_MICROPHONE
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.capture import capture_image
from sentry_mode.vision.preview import PreviewSessions
from sentry_mode.vision.recording import MAX_MESSAGE_BYTES, MEDIA_TYPES
from sentry_mode.vision.scheduler import InferenceScheduler
from sentry_mode.vision.stream import VideoStream

if TYPE_CHECKING:  # the satellite subsystem is imported only where it is used
    from sentry_mode.satellites.service import SatelliteService

logger = logging.getLogger(__name__)


class SpeechRequest(SoundboardMessage):
    """A message spoken now; the soundboard saves exactly the same fields."""


class SoundboardReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[0-9a-f]{16}$")


class CaptureReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=80)


class SoundReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=64)


class PreviewStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=81)


class PreviewReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str = Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")


MANUAL_VIDEO_SECONDS = 60


class HardwareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera: int | str
    camera_width: int = Field(gt=0, le=7680)
    camera_height: int = Field(gt=0, le=4320)
    camera_fps: float = Field(gt=0, le=240)
    camera_fourcc: str = Field(max_length=4)
    camera_exposure: int = Field(ge=0, le=100000)
    microphone: str = Field(min_length=1, max_length=200)
    speaker: str = Field(min_length=1, max_length=200)
    speaker_volume: int = Field(ge=0, le=100)


class ToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class OutputLevelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: int = Field(ge=0, le=100)


class SentryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    config: SentryConfig
    revision: int = Field(ge=0)
    clear_telegram_token: StrictBool = False


class SentryV2Update(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    config: SentryConfigV2
    revision: int = Field(ge=0)
    clear_telegram_token: StrictBool = False


class SimulationRequest(BaseModel):
    """A rule and the observations to play through it; nothing is executed."""

    model_config = ConfigDict(extra="forbid")
    rule: RuleV2
    samples: list[dict] = Field(max_length=500)


class TestModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class NodeControls:
    """Serialize hardware access and own the runtime started by this dashboard."""

    def __init__(self, config: Settings):
        self.config = config
        self.hardware_lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.talk = TalkStream(config, self.audio_lock)
        self.monitor = AudioMonitor()
        self.https_port: int | None = None
        self.tls_ca: Path | None = None
        # One model for every camera; each camera asks it for a share.
        self.inference = InferenceScheduler(config.detection)
        self.video = VideoStream(
            config.camera, self.hardware_lock, config.detection, scheduler=self.inference
        )
        self.soundboard = Soundboard(config.soundboard_file, config.soundboard_directory)
        self.sources = SourceRegistry()
        for record in legacy_sources(config):
            self.sources.register(record)
        self.cameras = SourceManager(self.sources)
        self.cameras.add(self.video, register=False)
        self.sentry = Sentry(config, self.video, self.audio_lock, self.sources, self.cameras)
        self.previews = PreviewSessions(self.cameras, self.video, self.sources)
        self.satellites_error: str | None = None
        self.satellites = self._open_satellites()
        self.runtime_lock = threading.Lock()
        self.stopped = threading.Event()
        self.shutdown_requested = threading.Event()
        self.thread: threading.Thread | None = None
        self.runtime_error: str | None = None
        self.cached_status: dict | None = None
        self.checked = 0.0
        self.recording_lock = threading.Lock()
        self.recording: dict | None = None
        self.recording_result: dict | None = None

    def _open_satellites(self) -> "SatelliteService | None":
        """Build the satellite subsystem, but only on a hub that was told to want one.

        The import is deferred on purpose: a hub with satellites switched off loads no
        broker client, opens no trust store and starts no threads. A link that refuses to
        start is reported through the status rather than taken as a reason to keep the
        dashboard down, because the cameras on this node work whether or not it has any.
        """
        if not self.config.satellites.enabled:
            return None
        from sentry_mode.satellites.service import build

        service = build(
            self.config.satellites,
            self.sources,
            on_event=self.sentry.observe_event,
            cameras=self.cameras,
            inference=self.inference,
            detection=self.config.detection,
        )
        try:
            assert service is not None
            service.start()
        except Exception as exc:
            self.satellites_error = str(exc)
            logger.exception("The satellite link could not be started")
        return service

    def satellite_status(self) -> dict:
        """What the subsystem reports, with the reason it is absent when it is."""
        if self.satellites is None:
            return {"enabled": False, "error": self.satellites_error, "nodes": []}
        return {**self.satellites.status(), "error": self.satellites_error}

    def satellites_overview(self) -> dict:
        """The satellites page: nodes, sources, readings and the last configuration sent."""
        from sentry_mode.satellites.api import overview

        return overview(self.satellite_status())

    def configure_satellite(self, body: dict | None) -> dict:
        from sentry_mode.satellites.api import ConfigureRequest

        if self.satellites is None:
            raise BlockingIOError("Satellites are switched off on this hub.")
        request = ConfigureRequest.model_validate(body)
        sent = self.satellites.configure(request.node_id, request.entries())
        return {
            "message": f"Configuration {sent['revision']} sent to {request.node_id}.",
            "configuration": sent,
        }

    def stop_satellites(self) -> None:
        if self.satellites is not None:
            self.satellites.stop()
            self.satellites = None

    def runtime_status(self) -> dict:
        return {
            "running": self.thread is not None and self.thread.is_alive(),
            "error": self.runtime_error,
        }

    def start_runtime(self) -> dict:
        with self.runtime_lock:
            if not self.runtime_status()["running"]:
                if self.video.status()["capture_running"]:
                    raise BlockingIOError(
                        "Stop video and disarm Sentry before starting the idle runtime."
                    )
                self.stopped.clear()
                self.runtime_error = None

                def worker():
                    try:
                        run(self.config, self.stopped, self.hardware_lock)
                    except Exception as exc:
                        self.runtime_error = str(exc)
                        logger.exception("Dashboard runtime failed")

                self.thread = threading.Thread(target=worker, name="sentry-mode-runtime")
                self.thread.start()
            return self.runtime_status()

    def stop_runtime(self) -> dict:
        with self.runtime_lock:
            self.stopped.set()
            if self.thread is not None:
                self.thread.join(timeout=30)
            return self.runtime_status()

    def get(self, path: str) -> dict:
        if path == "/api/sentry/status":
            return self.sentry.status()
        if path == "/api/sentry/config":
            return self.sentry.configuration()
        if path == "/api/sentry/v2/config":
            return self.sentry.configuration_v2()
        if path == "/api/satellites":
            return self.satellites_overview()
        if path == "/api/talk/config":
            return {"https_port": self.https_port, "local_ca": self.tls_ca is not None}
        if path == "/api/config":
            return self.config.model_dump(mode="json")
        if path == "/api/runtime":
            return self.runtime_status()
        if path == "/api/speech/voices":
            return available_voices(self.config)
        if path == "/api/soundboard":
            return self.soundboard.listing()
        if path == "/api/hardware":
            saved = config_path()
            return {
                "cameras": sorted({*list_cameras(), str(self.config.camera.device)}),
                "microphones": [
                    asdict(d) for d in Microphone(self.config.microphone).list_devices()
                ],
                "speakers": [asdict(d) for d in Speaker(self.config.speaker).list_devices()],
                "camera": str(self.config.camera.device),
                "camera_width": self.config.camera.width,
                "camera_height": self.config.camera.height,
                "camera_fps": self.config.camera.fps,
                "camera_fourcc": self.config.camera.fourcc,
                "camera_exposure": self.config.camera.exposure,
                "microphone": self.config.microphone.device,
                "speaker": self.config.speaker.device,
                "speaker_volume": self.config.speaker.volume,
                "output_level": Speaker(self.config.speaker).output_level(),
                "saved_to": str(saved) if saved else None,
            }
        if path == "/api/sounds":
            return self.sentry.sounds.listing()
        if path == "/api/captures":
            return self.sentry.captures.listing()
        if path == "/api/streams":
            # What the node is sending or receiving live, for the nav mark on every page.
            return {
                "video": bool(self.video.status()["running"]),
                "audio": self.monitor.listening > 0 or self.talk.active,
            }
        if path == "/api/video/status":
            return self.video_status()
        if path == "/api/cameras":
            return self.previews.listing()
        if path == "/api/camera/list":
            return {"devices": list_cameras(), "configured": self.config.camera.device}
        if path == "/api/audio/list":
            return {
                "microphones": [
                    asdict(d) for d in Microphone(self.config.microphone).list_devices()
                ],
                "speakers": [asdict(d) for d in Speaker(self.config.speaker).list_devices()],
                "configured": {
                    "microphone": self.config.microphone.device,
                    "speaker": self.config.speaker.device,
                },
            }
        if path == "/api/status":
            if not self.hardware_lock.acquire(blocking=False):
                # A live stream owns the camera; don't open it a second time or block HTTP.
                return {
                    "camera_available": self.video.status()["capture_running"],
                    "microphone_available": Microphone(self.config.microphone).is_available(),
                    "speaker_available": Speaker(self.config.speaker).is_available(),
                    "network_available": network_available(),
                    "diagnostics": {
                        "camera": "Camera is in use; stop video and Sentry to run camera tests."
                    },
                }
            try:
                if self.cached_status is None or time.monotonic() - self.checked > 10:
                    self.cached_status = asdict(inspect_hardware(self.config))
                    self.checked = time.monotonic()
                return self.cached_status
            finally:
                self.hardware_lock.release()
        raise KeyError(path)

    def speak(self, payload: SoundboardMessage) -> dict:
        if not self.audio_lock.acquire(blocking=False):
            raise BlockingIOError("Speaker is busy; wait for the current audio operation.")
        try:
            return speak(
                self.config,
                payload.text,
                payload.voice,
                payload.rate,
                stop_event=self.shutdown_requested,
                effects=payload.effects,
            )
        finally:
            self.audio_lock.release()

    def save_to_soundboard(self, payload: SoundboardMessage) -> dict:
        saved = self.soundboard.save(payload)
        message = self.soundboard.get(saved["saved"])
        sample = self.soundboard.sample_path(message.id)
        if sample.is_file():
            return {**saved, "sample": True}
        try:
            render_sample(self.config, message, sample, stop_event=self.shutdown_requested)
        except (HardwareError, OSError, ValueError) as exc:
            # The message is saved either way; playing it will synthesize the sample.
            logger.warning("Could not render the sample of %s: %s", message.id, exc)
            return {**saved, "sample": False}
        return {**saved, "sample": True}

    def sample_audio_bytes(self, message_id: str) -> bytes:
        """The mp3 of a saved message, so a browser can preview it on its own speaker."""
        message = self.soundboard.get(message_id)
        sample = self.soundboard.sample_path(message.id)
        if not sample.is_file():
            render_sample(self.config, message, sample, stop_event=self.shutdown_requested)
        return sample_audio(sample, message.effects)

    def play_from_soundboard(self, message_id: str) -> dict:
        message = self.soundboard.get(message_id)
        sample = self.soundboard.sample_path(message.id)
        if not sample.is_file():
            # A message saved before samples existed, or one whose sample was lost.
            render_sample(self.config, message, sample, stop_event=self.shutdown_requested)
        if not self.audio_lock.acquire(blocking=False):
            raise BlockingIOError("Speaker is busy; wait for the current audio operation.")
        try:
            return play_sample(
                self.config,
                sample,
                stop_event=self.shutdown_requested,
                effects=message.effects,
            )
        finally:
            self.audio_lock.release()

    def save_message(self, data: bytes) -> dict:
        name = self.sentry.captures.save_message(data)
        return {"message": "Message saved to Captures.", "name": name}

    def video_status(self) -> dict:
        recording = self.recording
        active = recording is not None and recording["thread"].is_alive()
        return {
            **self.video.status(),
            "recording": {
                "active": active,
                "started": recording["started"] if active else None,
                "max_seconds": MANUAL_VIDEO_SECONDS,
                "result": self.recording_result,
            },
        }

    def start_video_recording(self) -> dict:
        with self.recording_lock:
            if self.recording is not None and self.recording["thread"].is_alive():
                raise BlockingIOError("A video is already recording.")
            if not self.video.status()["running"]:
                raise BlockingIOError("Start video before recording.")
            stop = threading.Event()
            try:
                microphone, sound_error = Microphone(self.config.microphone).ffmpeg_input(), None
            except HardwareError as exc:
                microphone, sound_error = None, str(exc)
            lease = self.video.hold_recording("manual recording")

            def record():
                try:
                    name, audio_error = self.sentry.captures.record_video(
                        self.video.latest_frame,
                        MANUAL_VIDEO_SECONDS,
                        "manual recording",
                        stop,
                        size=self.video.recording_size(),
                        microphone=microphone,
                        meta={
                            "source_id": PRIMARY_CAMERA,
                            "origin": "local",
                            "timing": "manual",
                            "audio_source_id": PRIMARY_MICROPHONE if microphone else None,
                        },
                    )
                    missing = sound_error or audio_error
                    self.recording_result = {
                        "message": "Saved · "
                        + name
                        + (f" · no sound: {missing}" if missing else ""),
                        "error": False,
                    }
                except Exception as exc:
                    self.recording_result = {
                        "message": f"Video recording failed: {exc}",
                        "error": True,
                    }
                finally:
                    lease.release()

            self.recording_result = None
            thread = threading.Thread(target=record, name="sentry-mode-manual-video")
            self.recording = {"thread": thread, "stop": stop, "started": time.time()}
            thread.start()
            return self.video_status()

    def stop_video_recording(self) -> dict:
        with self.recording_lock:
            if self.recording is not None:
                self.recording["stop"].set()
                self.recording["thread"].join(timeout=30)
            return self.video_status()

    def set_hardware(self, request: HardwareRequest) -> dict:
        camera = CameraConfig(
            **{
                **self.config.camera.model_dump(),
                "device": request.camera,
                "width": request.camera_width,
                "height": request.camera_height,
                "fps": request.camera_fps,
                "fourcc": request.camera_fourcc,
                "exposure": request.camera_exposure,
            }
        )
        if camera.device != self.config.camera.device and (
            self.video.status()["capture_running"] or self.sentry.status()["armed"]
        ):
            raise BlockingIOError("Stop video and disarm Sentry before changing the camera.")
        microphone = AudioConfig(
            **{**self.config.microphone.model_dump(), "device": request.microphone}
        )
        speaker = SpeakerConfig(
            **{
                **self.config.speaker.model_dump(),
                "device": request.speaker,
                "volume": request.speaker_volume,
            }
        )
        saved = config_path()
        if saved is not None:
            save_sections(
                saved,
                {
                    "camera": {
                        "device": camera.device,
                        "width": camera.width,
                        "height": camera.height,
                        "fps": camera.fps,
                        "fourcc": camera.fourcc,
                        "exposure": camera.exposure,
                    },
                    "microphone": {"device": microphone.device},
                    "speaker": {"device": speaker.device, "volume": speaker.volume},
                },
            )
        # Adapters read these objects on every use, so the change applies to the next
        # capture, recording or announcement without a restart.
        # A running capture compares its settings each pass, so a size or rate change
        # reopens the device on the next frame rather than waiting for a restart.
        for field in ("device", "width", "height", "fps", "fourcc", "exposure"):
            setattr(self.config.camera, field, getattr(camera, field))
        self.config.microphone.device = microphone.device
        self.config.speaker.device, self.config.speaker.volume = speaker.device, speaker.volume
        self.cached_status = None
        return {
            "message": "Devices saved." if saved else "Devices applied until the next restart.",
            "saved_to": str(saved) if saved else None,
        }

    def post(self, path: str, body: dict | None = None) -> dict | bytes:
        if self.shutdown_requested.is_set():
            raise HardwareError("Server is shutting down.")
        if path == "/api/sentry/config":
            update = SentryUpdate.model_validate(body)
            return self.sentry.update(update.config, update.revision, update.clear_telegram_token)
        if path == "/api/sentry/test-mode":
            return self.sentry.set_test_mode(TestModeRequest.model_validate(body).enabled)
        if path == "/api/sentry/rules/test":
            return self.sentry.test(Rule.model_validate(body))
        if path == "/api/sentry/v2/config":
            v2 = SentryV2Update.model_validate(body)
            return self.sentry.update_v2(v2.config, v2.revision, v2.clear_telegram_token)
        if path == "/api/sentry/v2/rules/test":
            return self.sentry.test(RuleV2.model_validate(body))
        if path == "/api/events/simulate":
            simulation = SimulationRequest.model_validate(body)
            return self.sentry.simulate(simulation.rule, simulation.samples)
        if path == "/api/satellites/configure":
            return self.configure_satellite(body)
        if path == "/api/sentry/start":
            if self.runtime_status()["running"]:
                raise BlockingIOError("Stop the idle runtime before starting Sentry.")
            return self.sentry.arm()
        if path == "/api/sentry/stop":
            return self.sentry.disarm()
        if path == "/api/talk/start":
            return self.talk.start(VoiceEffects.model_validate(body if body is not None else {}))
        if path == "/api/video/start":
            self.cached_status = None
            self.video.start()
            return self.video_status()
        if path == "/api/video/detection":
            self.video.set_detection(ToggleRequest.model_validate(body).enabled)
            return self.video_status()
        if path == "/api/video/stop":
            self.cached_status = None
            self.stop_video_recording()
            self.video.stop()
            return self.video_status()
        if path == "/api/cameras/preview/start":
            return self.previews.open(PreviewStart.model_validate(body).source_id)
        if path == "/api/cameras/preview/renew":
            return self.previews.renew(PreviewReference.model_validate(body).session)
        if path == "/api/cameras/preview/stop":
            return self.previews.close(PreviewReference.model_validate(body).session)
        if path == "/api/video/record/start":
            return self.start_video_recording()
        if path == "/api/video/record/stop":
            return self.stop_video_recording()
        if path == "/api/speech":
            return self.speak(SpeechRequest.model_validate(body))
        if path == "/api/soundboard":
            return self.save_to_soundboard(SoundboardMessage.model_validate(body))
        if path == "/api/soundboard/delete":
            return self.soundboard.delete(SoundboardReference.model_validate(body).id)
        if path == "/api/hardware":
            return self.set_hardware(HardwareRequest.model_validate(body))
        if path == "/api/audio/level":
            level = OutputLevelRequest.model_validate(body).level
            speaker = Speaker(self.config.speaker)
            return {
                "message": f"Speaker output level set to {level}% of unity.",
                **speaker.set_output_level(level),
            }
        if path == "/api/sounds/delete":
            sound = SoundReference.model_validate(body).id
            used = [
                rule.name
                for rule in self.sentry.config.rules
                if any(getattr(action, "sound", None) == sound for action in rule.actions)
            ]
            if used:
                raise BlockingIOError(
                    "This audio file is used by " + ", ".join(used) + "; change those rules first."
                )
            return self.sentry.sounds.delete(sound)
        if path == "/api/sounds/play":
            sound = SoundReference.model_validate(body).id
            self.sentry.sounds.path(sound)
            if not self.audio_lock.acquire(blocking=False):
                raise BlockingIOError("Speaker is busy; wait for the current audio operation.")
            try:
                return self.sentry.sounds.play(
                    self.config, sound, stop_event=self.shutdown_requested
                )
            finally:
                self.audio_lock.release()
        if path == "/api/tunes/play":
            tune = TuneAction.model_validate({**(body or {}), "type": "tune"})
            if not self.audio_lock.acquire(blocking=False):
                raise BlockingIOError("Speaker is busy; wait for the current audio operation.")
            try:
                play_tune(
                    self.config.speaker,
                    tune.tune,
                    tune.repeat,
                    tune.volume,
                    tune.pitch,
                    stop_event=self.shutdown_requested,
                )
            finally:
                self.audio_lock.release()
            return {"message": f"Played {TUNES[tune.tune][0]} on the node speaker."}
        if path == "/api/captures/delete":
            return self.sentry.captures.delete(CaptureReference.model_validate(body).name)
        if path == "/api/soundboard/play":
            return self.play_from_soundboard(SoundboardReference.model_validate(body).id)
        if path == "/api/camera/capture" and self.video.status()["capture_running"]:
            return self.video.snapshot()
        if path == "/api/config/validate":
            Settings.model_validate(self.config.model_dump())
            return {"message": "Configuration valid"}
        if path == "/api/runtime/start":
            return self.start_runtime()
        if path == "/api/runtime/stop":
            return self.stop_runtime()
        if path not in {
            "/api/camera/test",
            "/api/camera/capture",
            "/api/audio/test-input",
            "/api/audio/test-output",
        }:
            raise KeyError(path)
        lock = self.audio_lock if path.startswith("/api/audio/") else self.hardware_lock
        if not lock.acquire(blocking=False):
            raise BlockingIOError("Hardware is busy; wait for the current operation to finish.")
        try:
            self.cached_status = None
            if path == "/api/camera/test":
                camera = Camera(self.config.camera)
                try:
                    latency = camera.measure_latency()
                    return {
                        "message": (
                            f"Camera test successful: {latency['effective_fps']} frames/second"
                            f" delivered, first frame after {latency['first_frame_ms']} ms."
                        ),
                        **camera.info(),
                        "latency": latency,
                    }
                finally:
                    camera.close()
            if path == "/api/camera/capture":
                with tempfile.TemporaryDirectory(prefix="sentry-mode-web-") as directory:
                    output = Path(directory) / "frame.jpg"
                    capture_image(Camera(self.config.camera), output)
                    return output.read_bytes()
            if path == "/api/audio/test-input":
                return {
                    "message": "Microphone capture successful",
                    **Microphone(self.config.microphone).test_input(),
                }
            Speaker(self.config.speaker).test_output()
            return {"message": "Test tone played; confirm audibility at the configured speaker."}
        finally:
            lock.release()


JSON_POSTS = {
    "/api/cameras/preview/start",
    "/api/cameras/preview/renew",
    "/api/cameras/preview/stop",
    "/api/speech",
    "/api/video/detection",
    "/api/sentry/config",
    "/api/sentry/test-mode",
    "/api/sentry/rules/test",
    "/api/sentry/v2/config",
    "/api/sentry/v2/rules/test",
    "/api/events/simulate",
    "/api/satellites/configure",
    "/api/soundboard",
    "/api/soundboard/delete",
    "/api/soundboard/play",
    "/api/captures/delete",
    "/api/hardware",
    "/api/audio/level",
    "/api/sounds/delete",
    "/api/sounds/play",
    "/api/tunes/play",
}


def query(path: str) -> dict[str, str]:
    """The first value of each query parameter, for the few GET routes that take one."""
    return {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}


def make_handler(controls: NodeControls):
    page = files("sentry_mode").joinpath("web.html").read_bytes()
    tests_page = files("sentry_mode").joinpath("tests.html").read_bytes()
    sentry_page = files("sentry_mode").joinpath("sentry.html").read_bytes()
    satellites_page = files("sentry_mode").joinpath("satellites.html").read_bytes()
    stylesheet = files("sentry_mode").joinpath("dashboard.css").read_bytes()
    scripts = {
        "/dashboard.js": files("sentry_mode").joinpath("dashboard.js").read_bytes(),
        "/talk.js": files("sentry_mode").joinpath("talk.js").read_bytes(),
        "/pcm-worklet.js": files("sentry_mode").joinpath("pcm-worklet.js").read_bytes(),
        "/voice-effects.js": files("sentry_mode").joinpath("voice-effects.js").read_bytes(),
        "/sentry.js": files("sentry_mode").joinpath("sentry.js").read_bytes(),
        "/soundboard.js": files("sentry_mode").joinpath("soundboard.js").read_bytes(),
        "/listen.js": files("sentry_mode").joinpath("listen.js").read_bytes(),
        "/satellites.js": files("sentry_mode").joinpath("satellites.js").read_bytes(),
    }

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except ssl.SSLError:
                logger.info("HTTPS client closed the connection or did not trust its certificate.")

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def respond(
            self, data: dict | bytes, status: int = 200, content_type: str = "application/json"
        ) -> None:
            content = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            ancestors = (
                "'self' " + " ".join(controls.config.web_frame_origins)
                if controls.config.web_frame_origins
                else "'none'"
            )
            self.send_header("Content-Security-Policy", "frame-ancestors " + ancestors)
            if content_type == "image/jpeg":
                self.send_header("Content-Disposition", 'inline; filename="sentry-mode-frame.jpg"')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == "/":
                self.respond(page, content_type="text/html; charset=utf-8")
            elif self.path in {"/hardware", "/hardware/", "/tests", "/tests/"}:
                self.respond(tests_page, content_type="text/html; charset=utf-8")
            elif self.path in {"/sentry", "/sentry/"}:
                self.respond(sentry_page, content_type="text/html; charset=utf-8")
            elif self.path in {"/satellites", "/satellites/"}:
                self.respond(satellites_page, content_type="text/html; charset=utf-8")
            elif self.path in scripts:
                self.respond(scripts[self.path], content_type="text/javascript; charset=utf-8")
            elif self.path == "/dashboard.css":
                self.respond(stylesheet, content_type="text/css; charset=utf-8")
            elif self.path == "/local-ca.crt" and controls.tls_ca is not None:
                self.respond(
                    controls.tls_ca.read_bytes(), content_type="application/x-x509-ca-cert"
                )
            elif self.path == "/api/video":
                self.stream_video()
            elif self.path.startswith("/api/cameras/preview/stream?"):
                self.stream_preview(query(self.path).get("session", ""))
            elif self.path.startswith("/api/cameras/snapshot?"):
                source_id = query(self.path).get("source_id", "")
                self.execute(lambda: controls.previews.snapshot(source_id))
            # The browser adds a cache-busting query when it reconnects to the microphone.
            elif self.path.partition("?")[0] == "/api/audio/monitor":
                self.stream_audio()
            elif self.path.startswith("/captures/"):
                self.send_capture(self.path.removeprefix("/captures/"))
            elif self.path.startswith("/soundboard/"):
                self.send_sample(self.path.removeprefix("/soundboard/"))
            else:
                self.execute(lambda: controls.get(self.path))

        def send_sample(self, name: str):
            # Only ids already on the board are served, so the name cannot leave the folder.
            try:
                data = controls.sample_audio_bytes(name.removesuffix(".mp3"))
            except (LookupError, HardwareError, OSError, ValueError) as exc:
                logger.info("Sample %s is unavailable: %s", name, exc)
                self.respond({"error": "That saved message has no sample."}, 404)
                return
            self.respond(data, content_type="audio/mpeg")

        def send_capture(self, name: str):
            try:
                path = controls.sentry.captures.path(name)
                size = path.stat().st_size
            except (LookupError, OSError):
                self.respond({"error": "Capture not found."}, 404)
                return
            # Phone browsers fetch video in byte ranges and will not play it otherwise.
            start, end, status = 0, size - 1, 200
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if match and size and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", MEDIA_TYPES[path.suffix[1:]])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f'inline; filename="{name}"')
            self.end_headers()
            try:
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (OSError, TimeoutError):
                pass  # Viewer closed the player or seeked elsewhere.

        def stream_audio(self):
            streaming = False
            try:
                with controls.monitor.listen(controls.config) as audio:
                    self.send_response(200)
                    self.send_header("Content-Type", f"audio/L16; rate={AUDIO_RATE}; channels=1")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    streaming = True
                    for block in audio:
                        self.wfile.write(block)
                        self.wfile.flush()
            except BlockingIOError as exc:
                self.respond({"error": str(exc)}, 409)
            except (HardwareError, OSError) as exc:
                # Once audio flows the headers are gone; a failure just closes the socket.
                if not streaming:
                    self.respond({"error": str(exc)}, 503)

        def stream_video(self):
            controls.video.add_viewer(1)
            try:
                self.stream_frames()
            finally:
                controls.video.add_viewer(-1)

        def stream_preview(self, token: str):
            try:
                frames = controls.previews.stream(token)
                first = next(frames, None)
            except LookupError as exc:
                self.respond({"error": str(exc)}, 404)
                return
            if first is None:
                self.respond({"error": "The camera has no picture yet; try again."}, 503)
                return
            self.write_frames(first, frames)

        def write_frames(self, first: bytes, frames):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                for jpeg in itertools.chain([first], frames):
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode()
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                    self.wfile.flush()
                self.wfile.write(b"--frame--\r\n")
            except (OSError, TimeoutError):
                pass  # Viewer closed the page or stopped its image request.
            finally:
                frames.close()

        def stream_frames(self):
            if not controls.video.status()["running"]:
                self.respond({"error": "Start video before requesting the stream."}, 409)
                return
            first = controls.video.next_frame(-1)
            if first is None:
                self.respond({"error": controls.video.error or "Video is unavailable."}, 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            frame = first
            try:
                while frame is not None:
                    sequence, jpeg = frame
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode()
                        + b"\r\n\r\n"
                        + jpeg
                        + b"\r\n"
                    )
                    self.wfile.flush()
                    frame = controls.video.next_frame(sequence)
                self.wfile.write(b"--frame--\r\n")
            except (OSError, TimeoutError):
                pass  # Viewer closed the page or stopped its image request.

        def do_POST(self):
            # A custom header and same-origin request prevent cross-site form actions.
            origin = self.headers.get("Origin")
            if (
                self.headers.get("X-Sentry-Mode-Control") != "1"
                or origin is not None
                and urlsplit(origin).netloc != self.headers.get("Host")
            ):
                self.respond({"error": "Controls require a same-origin dashboard request."}, 403)
                return
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self.respond({"error": "Chunked requests are not supported."}, 400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = MAX_CHUNK if self.path == "/api/talk/chunk" else 8192
                if self.path in {
                    "/api/sentry/config",
                    "/api/sentry/v2/config",
                    "/api/events/simulate",
                }:
                    limit = 262144
                if self.path == "/api/satellites/configure":
                    limit = 65536
                if self.path == "/api/sounds/upload":
                    limit = MAX_UPLOAD_BYTES
                if self.path == "/api/captures/message":
                    limit = MAX_MESSAGE_BYTES
                if length < 0 or length > limit:
                    self.close_connection = True
                    self.respond({"error": f"Request body exceeds the {limit}-byte limit."}, 413)
                    return
                if self.path == "/api/talk/chunk":
                    if self.headers.get("Content-Type") != "application/octet-stream":
                        self.close_connection = True
                        self.respond(
                            {"error": "Audio chunks require application/octet-stream."}, 415
                        )
                        return
                    body_bytes = self.rfile.read(length)
                    if len(body_bytes) != length:
                        raise ValueError("Incomplete audio chunk")
                    token = self.headers.get("X-Sentry-Mode-Talk", "")
                    sequence = int(self.headers.get("X-Audio-Sequence", "-1"))
                    self.execute(lambda: controls.talk.chunk(token, sequence, body_bytes))
                elif self.path == "/api/sounds/upload":
                    if self.headers.get("Content-Type") != "application/octet-stream":
                        self.close_connection = True
                        self.respond(
                            {"error": "Audio uploads require application/octet-stream."}, 415
                        )
                        return
                    self.connection.settimeout(60)
                    body_bytes = self.rfile.read(length)
                    if len(body_bytes) != length:
                        raise ValueError("Incomplete audio upload")
                    name = unquote(self.headers.get("X-Sound-Name", ""))[:200]
                    self.execute(lambda: controls.sentry.sounds.add(name, body_bytes))
                elif self.path == "/api/captures/message":
                    if self.headers.get("Content-Type") != "application/octet-stream":
                        self.close_connection = True
                        self.respond(
                            {"error": "Recorded messages require application/octet-stream."}, 415
                        )
                        return
                    self.connection.settimeout(60)
                    body_bytes = self.rfile.read(length)
                    if len(body_bytes) != length:
                        raise ValueError("Incomplete recorded message")
                    self.execute(lambda: controls.save_message(body_bytes))
                elif self.path in {"/api/talk/stop", "/api/talk/cancel"} and not length:
                    token = self.headers.get("X-Sentry-Mode-Talk", "")
                    self.execute(
                        lambda: controls.talk.finish(token, cancel=self.path == "/api/talk/cancel")
                    )
                elif self.path in JSON_POSTS or (self.path == "/api/talk/start" and length):
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        self.respond({"error": "This action requires a JSON request."}, 415)
                        self.close_connection = True
                        return
                    body = json.loads(self.rfile.read(length))
                    self.execute(lambda: controls.post(self.path, body))
                elif length:
                    self.close_connection = True
                    self.respond({"error": "This action accepts no request body."}, 400)
                else:
                    self.execute(lambda: controls.post(self.path))
            except (ValueError, UnicodeError):
                self.close_connection = True
                self.respond({"error": "Invalid JSON or content length."}, 400)

        def execute(self, action):
            try:
                result = action()
                self.respond(
                    result,
                    content_type="image/jpeg" if isinstance(result, bytes) else "application/json",
                )
            except KeyError:
                self.respond({"error": "Unknown command"}, 404)
            except LookupError as exc:
                self.respond({"error": str(exc)}, 404)
            except BlockingIOError as exc:
                self.respond({"error": str(exc)}, 409)
            except SchemaUpgradeRequired as exc:
                self.respond(
                    {"error": str(exc), "code": exc.code, "reasons": list(exc.reasons)}, 409
                )
            except (ValueError, ValidationError) as exc:
                self.respond({"error": str(exc)}, 400)
            except (HardwareError, OSError, ImportError) as exc:
                logger.warning("Web command failed: %s", exc)
                self.respond({"error": str(exc)}, 503)
            except Exception:
                logger.exception("Unexpected web command failure")
                self.respond({"error": "Unexpected command failure; check server logs."}, 500)

        def log_message(self, format, *args):
            logger.info(format, *args)

    return Handler


def serve(
    config: Settings,
    host: str = "0.0.0.0",
    port: int = 8083,
    *,
    https_host: str | None = None,
    https_port: int | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
    tls_ca: Path | None = None,
) -> None:
    controls = NodeControls(config)
    controls.https_port, controls.tls_ca = https_port, tls_ca
    secure_host = https_host if https_host is not None else host
    tls = None
    if https_port is not None:
        if tls_cert is None or tls_key is None:
            raise ValueError("HTTPS requires --tls-cert and --tls-key.")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(tls_cert, tls_key)
    elif tls_cert or tls_key or tls_ca or https_host:
        raise ValueError("TLS options require --https-port.")
    if tls_ca:
        tls_ca.read_bytes()
    stopped = threading.Event()
    previous = {}

    def stop(signum, frame):
        stopped.set()
        controls.shutdown_requested.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        with ExitStack() as stack:
            handler = make_handler(controls)
            server = stack.enter_context(ThreadingHTTPServer((host, port), handler))
            secure = None
            secure_thread = None
            if tls is not None:
                assert https_port is not None

                class SecureServer(ThreadingHTTPServer):
                    def get_request(self):
                        connection, address = super().get_request()
                        connection.settimeout(10)
                        return tls.wrap_socket(
                            connection, server_side=True, do_handshake_on_connect=False
                        ), address

                secure = stack.enter_context(SecureServer((secure_host, https_port), handler))
                secure_thread = threading.Thread(
                    target=secure.serve_forever, name="sentry-mode-https"
                )
                secure_thread.start()
                logger.info("Serving phone controls over HTTPS on %s:%s", secure_host, https_port)
            server.daemon_threads = False
            server.timeout = 0.5
            logger.info("Serving Sentry Mode on %s:%s", host, port)
            try:
                while not stopped.is_set():
                    server.handle_request()
            finally:
                controls.shutdown_requested.set()
                if secure:
                    secure.shutdown()
                    assert secure_thread is not None
                    secure_thread.join()
                controls.talk.close()
                controls.previews.close_all()
                controls.stop_video_recording()
                controls.sentry.disarm()
                controls.video.close()
                controls.cameras.close()
                controls.inference.close()
                controls.stop_satellites()
                controls.stop_runtime()
    finally:
        controls.stop_runtime()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        logging.shutdown()

"""HTTP controls using the same configuration and hardware adapters as the CLI."""

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
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from sentry_node.app import run
from sentry_node.audio.effects import VoiceEffects
from sentry_node.audio.soundboard import Soundboard, SoundboardMessage
from sentry_node.audio.sounds import MAX_UPLOAD_BYTES
from sentry_node.audio.speech import available_voices, speak
from sentry_node.audio.talk import MAX_CHUNK, TalkStream
from sentry_node.audio.tunes import TUNES, play_tune
from sentry_node.config import Settings
from sentry_node.core.errors import HardwareError
from sentry_node.hardware.camera import Camera, list_cameras
from sentry_node.hardware.microphone import Microphone
from sentry_node.hardware.speaker import Speaker
from sentry_node.hardware.status import inspect_hardware, network_available
from sentry_node.sentry.config import SentryConfig, TuneAction
from sentry_node.sentry.engine import Sentry
from sentry_node.vision.capture import capture_image
from sentry_node.vision.recording import MAX_MESSAGE_BYTES, MEDIA_TYPES
from sentry_node.vision.stream import VideoStream

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


MANUAL_VIDEO_SECONDS = 60


class DetectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class SentryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    config: SentryConfig
    revision: int = Field(ge=0)
    clear_telegram_token: StrictBool = False


class NodeControls:
    """Serialize hardware access and own the runtime started by this dashboard."""

    def __init__(self, config: Settings):
        self.config = config
        self.hardware_lock = threading.Lock()
        self.audio_lock = threading.Lock()
        self.talk = TalkStream(config, self.audio_lock)
        self.https_port: int | None = None
        self.tls_ca: Path | None = None
        self.video = VideoStream(config.camera, self.hardware_lock, config.detection)
        self.sentry = Sentry(config, self.video, self.audio_lock)
        self.soundboard = Soundboard(config.soundboard_file)
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

                self.thread = threading.Thread(target=worker, name="sentry-node-runtime")
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
        if path == "/api/sounds":
            return self.sentry.sounds.listing()
        if path == "/api/captures":
            return self.sentry.captures.listing()
        if path == "/api/video/status":
            return self.video_status()
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
            self.video.add_recording(1)

            def record():
                try:
                    name, audio_error = self.sentry.captures.record_video(
                        self.video.latest_frame,
                        MANUAL_VIDEO_SECONDS,
                        "manual recording",
                        stop,
                        size=self.video.recording_size(),
                        microphone=microphone,
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
                    self.video.add_recording(-1)

            self.recording_result = None
            thread = threading.Thread(target=record, name="sentry-node-manual-video")
            self.recording = {"thread": thread, "stop": stop, "started": time.time()}
            thread.start()
            return self.video_status()

    def stop_video_recording(self) -> dict:
        with self.recording_lock:
            if self.recording is not None:
                self.recording["stop"].set()
                self.recording["thread"].join(timeout=30)
            return self.video_status()

    def post(self, path: str, body: dict | None = None) -> dict | bytes:
        if self.shutdown_requested.is_set():
            raise HardwareError("Server is shutting down.")
        if path == "/api/sentry/config":
            update = SentryUpdate.model_validate(body)
            return self.sentry.update(update.config, update.revision, update.clear_telegram_token)
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
            self.video.set_detection(DetectionRequest.model_validate(body).enabled)
            return self.video_status()
        if path == "/api/video/stop":
            self.cached_status = None
            self.stop_video_recording()
            self.video.stop()
            return self.video_status()
        if path == "/api/video/record/start":
            return self.start_video_recording()
        if path == "/api/video/record/stop":
            return self.stop_video_recording()
        if path == "/api/speech":
            return self.speak(SpeechRequest.model_validate(body))
        if path == "/api/soundboard":
            return self.soundboard.save(SoundboardMessage.model_validate(body))
        if path == "/api/soundboard/delete":
            return self.soundboard.delete(SoundboardReference.model_validate(body).id)
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
                    self.shutdown_requested,
                )
            finally:
                self.audio_lock.release()
            return {"message": f"Played {TUNES[tune.tune][0]} on the node speaker."}
        if path == "/api/captures/delete":
            return self.sentry.captures.delete(CaptureReference.model_validate(body).name)
        if path == "/api/soundboard/play":
            return self.speak(self.soundboard.get(SoundboardReference.model_validate(body).id))
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
                with Camera(self.config.camera) as camera:
                    for _ in range(5):
                        camera.capture_frame()
                    return {"message": "Camera test successful", **camera.info()}
            if path == "/api/camera/capture":
                with tempfile.TemporaryDirectory(prefix="sentry-node-web-") as directory:
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
    "/api/speech",
    "/api/video/detection",
    "/api/sentry/config",
    "/api/soundboard",
    "/api/soundboard/delete",
    "/api/soundboard/play",
    "/api/captures/delete",
    "/api/sounds/delete",
    "/api/sounds/play",
    "/api/tunes/play",
}


def make_handler(controls: NodeControls):
    page = files("sentry_node").joinpath("web.html").read_bytes()
    tests_page = files("sentry_node").joinpath("tests.html").read_bytes()
    sentry_page = files("sentry_node").joinpath("sentry.html").read_bytes()
    stylesheet = files("sentry_node").joinpath("dashboard.css").read_bytes()
    scripts = {
        "/dashboard.js": files("sentry_node").joinpath("dashboard.js").read_bytes(),
        "/talk.js": files("sentry_node").joinpath("talk.js").read_bytes(),
        "/pcm-worklet.js": files("sentry_node").joinpath("pcm-worklet.js").read_bytes(),
        "/voice-effects.js": files("sentry_node").joinpath("voice-effects.js").read_bytes(),
        "/sentry.js": files("sentry_node").joinpath("sentry.js").read_bytes(),
        "/soundboard.js": files("sentry_node").joinpath("soundboard.js").read_bytes(),
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
                self.send_header("Content-Disposition", 'inline; filename="sentry-node-frame.jpg"')
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.path == "/":
                self.respond(page, content_type="text/html; charset=utf-8")
            elif self.path in {"/tests", "/tests/"}:
                self.respond(tests_page, content_type="text/html; charset=utf-8")
            elif self.path in {"/sentry", "/sentry/"}:
                self.respond(sentry_page, content_type="text/html; charset=utf-8")
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
            elif self.path.startswith("/captures/"):
                self.send_capture(self.path.removeprefix("/captures/"))
            else:
                self.execute(lambda: controls.get(self.path))

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

        def stream_video(self):
            controls.video.add_viewer(1)
            try:
                self.stream_frames()
            finally:
                controls.video.add_viewer(-1)

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
                self.headers.get("X-Sentry-Node-Control") != "1"
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
                if self.path == "/api/sentry/config":
                    limit = 262144
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
                    token = self.headers.get("X-Sentry-Node-Talk", "")
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
                    token = self.headers.get("X-Sentry-Node-Talk", "")
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
                    target=secure.serve_forever, name="sentry-node-https"
                )
                secure_thread.start()
                logger.info("Serving phone controls over HTTPS on %s:%s", secure_host, https_port)
            server.daemon_threads = False
            server.timeout = 0.5
            logger.info("Serving Sentry Node on %s:%s", host, port)
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
                controls.stop_video_recording()
                controls.sentry.disarm()
                controls.video.close()
                controls.stop_runtime()
    finally:
        controls.stop_runtime()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        logging.shutdown()

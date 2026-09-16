"""Shared camera capture with independent Sentry and optional JPEG preview demand."""

import threading
from _thread import LockType
from collections.abc import Callable

from sentry_mode.config import CameraConfig, DetectionConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.camera import Camera
from sentry_mode.vision.detection import DetectionWorker
from sentry_mode.vision.recording import VIDEO_FPS, VIDEO_MAX_WIDTH

# A device that re-enumerates takes a few seconds to come back; beyond that it is really gone.
RECONNECT_ATTEMPTS = 6
RECONNECT_PAUSE = 1.0


class VideoStream:
    def __init__(
        self, config: CameraConfig, camera_lock: LockType, detection: DetectionConfig | None = None
    ):
        self.config = config
        self.camera_lock = camera_lock
        self.lifecycle_lock = threading.Lock()
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.sequence = 0
        self.jpeg: bytes | None = None
        self.raw_frame = None
        self.error: str | None = None
        self.base_detection = detection or DetectionConfig()
        self.detection = DetectionWorker(self.base_detection)
        self.preview = False
        self.preview_detection = self.base_detection.enabled
        self.reconnects = 0
        self.monitoring = False
        self.monitor_fps = 2.0
        self.viewers = 0
        self.recordings = 0
        self.encoded_frames = 0
        self.capture_size: tuple[int, int] | None = None
        self.on_error: Callable[[str], None] | None = None

    def status(self) -> dict:
        active = self.thread is not None and self.thread.is_alive() and not self.stopped.is_set()
        return {
            "running": active and self.preview,
            "capture_running": active,
            "monitoring": self.monitoring,
            "error": self.error,
            "fps": min(self.config.fps, 10),
            "max_width": 960,
            "viewers": self.viewers,
            "encoded_frames": self.encoded_frames,
            "capture_size": self.capture_size,
            "reconnects": self.reconnects,
            "detection": self.detection.status(),
        }

    def set_detection(self, enabled: bool) -> dict:
        with self.lifecycle_lock:
            if self.monitoring and not enabled:
                raise BlockingIOError(
                    "Detection is required by Sentry. Disarm Sentry to disable it."
                )
            self.detection.configure(enabled, self.status()["capture_running"])
            self.preview_detection = enabled
            return self.status()

    def set_sentry(
        self, enabled: bool, fps: float = 2, confidence: float = 0.7, detect: bool = True
    ) -> dict:
        """Keep the camera running for Sentry, with the detector only when `detect` is set.

        Without the detector the camera just stays open, so a photo or a video asked for
        by a sensor rule starts at once instead of waiting for the camera to open.
        """
        with self.lifecycle_lock:
            if enabled and not detect:
                try:
                    self.monitor_fps, self.monitoring = fps, True
                    self._ensure_capture()
                except Exception:
                    self.monitoring = False
                    if not self.preview:
                        self._stop_capture()
                    raise
            elif enabled:
                self.detection.pause()
                self.detection.config = self.base_detection.model_copy(
                    update={
                        "max_fps": fps,
                        "confidence": min(self.base_detection.confidence, confidence),
                    }
                )
                if self.detection.detector is not None:
                    self.detection.detector.config = self.detection.config
                try:
                    self.detection.configure(True, False)
                    self.monitor_fps, self.monitoring = fps, True
                    self._ensure_capture()
                    self.detection.start()
                    if not self.detection.enabled:
                        raise HardwareError(self.detection.error or "Object detection failed.")
                except Exception:
                    self.monitoring = False
                    self._restore_detection()
                    if not self.preview:
                        self._stop_capture()
                    raise
            else:
                self.monitoring = False
                self._restore_detection()
                if not self.preview:
                    self._stop_capture()
            return self.status()

    def _restore_detection(self):
        self.detection.pause()
        self.detection.config = self.base_detection
        if self.detection.detector is not None:
            self.detection.detector.config = self.base_detection
        self.detection.configure(
            self.preview_detection, self.preview and self.status()["capture_running"]
        )

    def start(self) -> dict:
        with self.lifecycle_lock:
            self.preview = True
            try:
                self._ensure_capture()
            except Exception:
                self.preview = False
                raise
            return self.status()

    def _ensure_capture(self):
        if self.thread is not None and self.thread.is_alive():
            if self.stopped.is_set():
                raise BlockingIOError("Camera is still stopping; try again shortly.")
            return
        if not self.camera_lock.acquire(blocking=False):
            raise BlockingIOError("Camera is busy; finish the current camera operation first.")
        self.stopped.clear()
        self.ready.clear()
        self.error = None
        self.jpeg = None
        self.raw_frame = None
        self.detection.start()
        self.thread = threading.Thread(target=self._capture, name="sentry-mode-video")
        try:
            self.thread.start()
        except Exception:
            self.detection.pause()
            self.camera_lock.release()
            raise
        if not self.ready.wait(10):
            self.stopped.set()
            raise HardwareError("Video camera startup timed out.")
        if self.error:
            raise HardwareError(self.error)

    def _camera_config(self) -> CameraConfig:
        if self.preview:
            return self.config
        # A Sentry recording raises the resolution and rate only while it runs.
        width = min(self.config.width, VIDEO_MAX_WIDTH if self.recordings else 640)
        return self.config.model_copy(
            update={
                "width": width,
                "height": max(1, round(self.config.height * width / self.config.width)),
                "fps": min(self.config.fps, VIDEO_FPS) if self.recordings else self.monitor_fps,
            }
        )

    def _rate(self) -> float:
        if self.preview:
            return min(self.config.fps, 10)
        return min(self.config.fps, VIDEO_FPS) if self.recordings else self.monitor_fps

    def recording_size(self) -> tuple[int, int]:
        width = min(self.config.width, VIDEO_MAX_WIDTH)
        return width, max(1, round(self.config.height * width / self.config.width))

    def add_recording(self, delta: int):
        with self.condition:
            self.recordings = max(0, self.recordings + delta)

    def latest_frame(self):
        with self.condition:
            return None if self.stopped.is_set() else self.raw_frame

    def _frame(self, camera):
        """A frame, reopening the device first if it went away underneath us.

        A USB camera that re-enumerates leaves the open handle pointing at nothing, and
        every read fails until it is reopened. One dropped frame should cost the preview a
        moment, not the whole session.
        """
        failure = HardwareError("camera returned no frame")
        for attempt in range(RECONNECT_ATTEMPTS):
            if attempt:
                self.reconnects += 1
                camera.close()
                if self.stopped.wait(RECONNECT_PAUSE):
                    raise failure
                try:
                    camera.open()
                except HardwareError as exc:
                    # Still gone, or gone again: spend the next attempt on reopening it.
                    failure = exc
                    continue
            try:
                return camera.capture_frame()
            except HardwareError as exc:
                failure = exc
        raise failure

    def _capture(self):
        try:
            selected = self._camera_config().model_copy()
            with Camera(selected) as camera:
                for _ in range(5 if self.preview else 2):
                    frame = self._frame(camera)
                while not self.stopped.is_set():
                    desired = self._camera_config()
                    if selected != desired:
                        camera.close()
                        camera.config = desired
                        camera.open()
                        selected = desired.model_copy()
                        for _ in range(5 if self.preview else 2):
                            frame = self._frame(camera)
                    self.capture_size = (selected.width, selected.height)
                    self.detection.submit(frame)
                    with self.condition:
                        self.raw_frame = frame
                        render = self.preview and (self.viewers > 0 or self.jpeg is None)
                    if render:
                        jpeg = camera.encode_jpeg(self.detection.overlay(frame))
                        with self.condition:
                            if self.preview:
                                self.jpeg = jpeg
                                self.encoded_frames += 1
                                self.sequence += 1
                                self.condition.notify_all()
                    self.ready.set()
                    if self.stopped.wait(1 / self._rate()):
                        break
                    frame = self._frame(camera)
        except Exception as exc:
            self.error = str(exc)
            if self.on_error is not None:
                self.on_error(str(exc))
        finally:
            self.stopped.set()
            self.ready.set()
            self.detection.pause()
            self.camera_lock.release()
            with self.condition:
                self.raw_frame = None
                self.condition.notify_all()

    def stop(self) -> dict:
        """Hide preview; Sentry retains the camera if armed."""
        with self.lifecycle_lock:
            with self.condition:
                self.preview = False
                self.jpeg = None
                self.condition.notify_all()
            if not self.monitoring:
                self._stop_capture()
            return self.status()

    def _stop_capture(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=30)
        self.detection.pause()

    def close(self):
        with self.lifecycle_lock:
            self.preview = self.monitoring = False
            self._stop_capture()

    def snapshot(self) -> bytes:
        with self.condition:
            if self.jpeg is not None and self.preview:
                return self.jpeg
            frame = self.raw_frame
        if frame is None:
            raise HardwareError("No camera frame is available yet.")
        return Camera(self.config).encode_jpeg(self.detection.overlay(frame))

    def add_viewer(self, delta: int):
        with self.condition:
            self.viewers = max(0, self.viewers + delta)
            if delta > 0:
                self.jpeg = None  # New viewers wait for a current image, not an old cached frame.
            self.condition.notify_all()

    def next_frame(self, sequence: int) -> tuple[int, bytes] | None:
        with self.condition:
            self.condition.wait_for(
                lambda: (
                    (self.sequence != sequence and self.jpeg is not None)
                    or self.stopped.is_set()
                    or not self.preview
                ),
                timeout=5,
            )
            if (
                self.stopped.is_set()
                or not self.preview
                or self.sequence == sequence
                or self.jpeg is None
            ):
                return None
            return self.sequence, self.jpeg

"""A camera made of generated frames.

It exists to prove that more than one camera can share the model before a second real
camera does: it takes leases, runs only while someone holds one, and feeds the scheduler
like any other camera. Nothing creates one unless a test or a developer asks for it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from sentry_mode.config import DetectionConfig
from sentry_mode.sources.manager import DemandTable, Lease
from sentry_mode.vision.detection import DetectionWorker
from sentry_mode.vision.scheduler import InferenceScheduler

PREVIEW_FPS = 10.0


def plain(width: int, height: int) -> Callable[[int], Any]:
    import numpy as np

    def frame(sequence: int):
        image = np.zeros((height, width, 3), np.uint8)
        image[:, : (sequence % width) + 1] = 90
        return image

    return frame


class SyntheticCamera:
    def __init__(
        self,
        source_id: str,
        scheduler: InferenceScheduler,
        detection: DetectionConfig,
        *,
        frames: Callable[[int], Any] | None = None,
        display_name: str | None = None,
        width: int = 320,
        height: int = 240,
    ) -> None:
        self.source_id = source_id
        self.display_name = display_name or f"Synthetic camera {source_id}"
        self.base_detection = detection
        self.frames = frames or plain(width, height)
        self.lifecycle = threading.Lock()
        self.condition = threading.Condition()
        self.detection = DetectionWorker(
            detection.model_copy(update={"enabled": False}), scheduler, source_id
        )
        self.demands = DemandTable(source_id, on_change=self._apply)
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.stopped.set()
        self.rate = 0.0
        self.captured = 0
        self.raw_frame = None
        self.error: str | None = None

    def hold(
        self,
        purpose: str,
        owner: str,
        *,
        fps: float = 2.0,
        confidence: float | None = None,
        detect: bool = False,
    ) -> Lease:
        params = {"fps": fps, "detect": detect}
        if confidence is not None:
            params["confidence"] = confidence
        lease = self.demands.hold(purpose, owner, **params)
        if self.error is not None:
            error = self.error
            lease.release()
            raise RuntimeError(error)
        return lease

    def _apply(self) -> None:
        with self.lifecycle:
            leases = self.demands.leases()
            if not leases:
                self._stop()
                return
            self.rate = max(
                PREVIEW_FPS if lease.purpose == "preview" else lease.params["fps"]
                for lease in leases
            )
            if self.stopped.is_set():
                self.error = None
                self._start()
            watching = [lease for lease in leases if lease.params.get("detect")]
            if not watching:
                if self.detection.enabled:
                    self.detection.configure(False, False)
                return
            asked = [
                lease.params["confidence"] for lease in watching if "confidence" in lease.params
            ]
            wanted = self.base_detection.model_copy(
                update={
                    "enabled": True,
                    "max_fps": max(lease.params["fps"] for lease in watching),
                    "confidence": min(asked, default=self.base_detection.confidence),
                }
            )
            if wanted == self.detection.config and not self.detection.stopped.is_set():
                return
            self.detection.pause()
            self.detection.config = wanted
            try:
                self.detection.configure(True, True)
            except Exception as exc:
                self.detection.enabled, self.detection.error = False, str(exc)
            self.error = None if self.detection.enabled else self.detection.error

    def _start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self._run, args=(self.stopped,), name=f"sentry-mode-{self.source_id}"
        )
        self.thread.start()

    def _stop(self) -> None:
        self.stopped.set()
        self.detection.pause()
        if self.thread is not None:
            self.thread.join(timeout=5)
        with self.condition:
            self.raw_frame = None

    def _run(self, stopped: threading.Event) -> None:
        try:
            while not stopped.is_set():
                frame = self.frames(self.captured)
                self.captured += 1
                with self.condition:
                    self.raw_frame = frame
                self.detection.submit(frame)
                if stopped.wait(1 / max(self.rate, 0.1)):
                    break
        except Exception as exc:
            self.error = str(exc)
            stopped.set()

    def latest_frame(self):
        with self.condition:
            return self.raw_frame

    def status(self) -> dict:
        running = self.thread is not None and self.thread.is_alive() and not self.stopped.is_set()
        return {
            "source_id": self.source_id,
            "capture_running": running,
            "fps": self.rate,
            "error": self.error,
            "captured_frames": self.captured,
            "leases": self.demands.summary(),
            "detection": self.detection.status(),
        }

    def close(self) -> None:
        for lease in self.demands.leases():
            lease.release()
        with self.lifecycle:
            self._stop()


__all__ = ["SyntheticCamera", "plain"]

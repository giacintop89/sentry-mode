"""OpenCV DNN object detection and a latest-frame-only inference worker."""

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from sentry_mode.config import DetectionConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.vision.labels import CLASSES


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[float, float, float, float]  # normalized x1, y1, x2, y2


class ObjectDetector:
    def __init__(self, config: DetectionConfig):
        import cv2
        import numpy as np

        self.config = config
        if not config.model.is_file():
            raise HardwareError("Object model is missing. Run python3 scripts/setup_detection.py.")
        try:
            cv2.setNumThreads(2)  # Leave CPU capacity for camera and audio on the Pi.
            self.net = cv2.dnn.readNetFromONNX(str(config.model))
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except cv2.error as exc:
            raise HardwareError(f"Cannot load object model: {exc}") from exc
        grids, strides = [], []
        for stride in (8, 16, 32):
            y, x = np.mgrid[: 416 // stride, : 416 // stride]
            grids.append(np.stack([x, y], axis=-1).reshape(-1, 2))
            strides.append(np.full((x.size, 1), stride))
        self.grid = np.concatenate(grids)
        self.strides = np.concatenate(strides)

    def detect(self, frame) -> list[Detection]:
        import cv2
        import numpy as np

        height, width = frame.shape[:2]
        ratio = min(416 / width, 416 / height)
        padded = np.full((416, 416, 3), 114, dtype=np.float32)
        resized = cv2.resize(frame, (max(1, int(width * ratio)), max(1, int(height * ratio))))
        padded[: resized.shape[0], : resized.shape[1]] = resized
        # This pinned export expects unnormalized BGR with top-left letterboxing.
        self.net.setInput(np.ascontiguousarray(padded.transpose(2, 0, 1)[None], dtype=np.float32))
        raw = self.net.forward()[0]
        if raw.shape != (3549, 85):
            raise HardwareError(
                "Unexpected detector output; install the supported YOLOX Nano model."
            )
        scores = raw[:, 4:5] * raw[:, 5:]
        ids = scores.argmax(axis=1)
        confidence = scores[np.arange(len(ids)), ids]
        keep = np.flatnonzero(np.isfinite(raw).all(axis=1) & (confidence >= self.config.confidence))
        if not len(keep):
            return []
        centers = (raw[keep, :2] + self.grid[keep]) * self.strides[keep]
        sizes = np.exp(np.clip(raw[keep, 2:4], -20, 20)) * self.strides[keep]
        xy = (centers - sizes / 2) / ratio
        sizes = sizes / ratio
        boxes = np.concatenate([xy, sizes], axis=1)
        selected: list[int] = []
        for cls in np.unique(ids[keep]):
            group = np.flatnonzero(ids[keep] == cls)
            indices = cv2.dnn.NMSBoxes(
                boxes[group].tolist(),
                confidence[keep][group].tolist(),
                self.config.confidence,
                0.45,
            )
            selected.extend(group[int(i)] for i in np.asarray(indices).reshape(-1))
        result = []
        for index in sorted(selected, key=lambda i: -confidence[keep[i]])[:30]:
            x, y, w, h = boxes[index]
            box = (
                float(np.clip(x / width, 0, 1)),
                float(np.clip(y / height, 0, 1)),
                float(np.clip((x + w) / width, 0, 1)),
                float(np.clip((y + h) / height, 0, 1)),
            )
            if box[2] > box[0] and box[3] > box[1]:
                result.append(
                    Detection(CLASSES[ids[keep[index]]], float(confidence[keep[index]]), box)
                )
        return result


def annotate(frame, detections: list[Detection]):
    import cv2

    if not detections:
        return frame
    output = frame.copy()
    height, width = output.shape[:2]
    scale = max(0.45, width / 1500)
    for detection in detections:
        x1, y1, x2, y2 = detection.box
        p1, p2 = (
            (int(x1 * (width - 1)), int(y1 * (height - 1))),
            (int(x2 * (width - 1)), int(y2 * (height - 1))),
        )
        cv2.rectangle(output, p1, p2, (118, 218, 177), 2)
        label = f"{detection.label} {detection.confidence:.0%}"
        (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        x = min(p1[0], max(0, width - tw - 8))
        y = max(th + 8, p1[1])
        cv2.rectangle(
            output, (x, y - th - 8), (min(width - 1, x + tw + 8), y + baseline), (16, 27, 34), -1
        )
        cv2.putText(
            output,
            label,
            (x + 4, y - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (118, 218, 177),
            2,
            cv2.LINE_AA,
        )
    return output


class DetectionWorker:
    def __init__(self, config: DetectionConfig):
        self.config = config
        self.enabled = config.enabled
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.detector: ObjectDetector | None = None
        self.pending: tuple[Any, float] | None = None
        self.result: list[Detection] = []
        self.updated = 0.0
        self.inference_ms: float | None = None
        self.error: str | None = None
        self.on_result: Callable[[list[Detection], float], None] | None = None
        self.on_error: Callable[[str], None] | None = None

    def status(self) -> dict:
        with self.condition:
            fresh = time.monotonic() - self.updated < 1.5
            return {
                "enabled": self.enabled,
                "error": self.error,
                "available": self.config.model.is_file(),
                "model": "YOLOX Nano",
                "max_fps": self.config.max_fps,
                "inference_ms": self.inference_ms,
                "objects": [asdict(d) for d in self.result] if fresh and self.enabled else [],
            }

    def configure(self, enabled: bool, running: bool) -> None:
        if not enabled:
            self.enabled = False
            self.pause()
            self.error = None
            return
        if self.detector is None:
            self.detector = ObjectDetector(self.config)
        self.enabled = True
        self.error = None
        if running:
            self.start()

    def start(self) -> None:
        if not self.enabled or self.thread is not None and self.thread.is_alive():
            return
        try:
            if self.detector is None:
                self.detector = ObjectDetector(self.config)
            self.stopped.clear()
            self.thread = threading.Thread(target=self._run, name="sentry-mode-detection")
            self.thread.start()
        except Exception as exc:
            self.error = str(exc)
            self.enabled = False  # A detector failure must not stop plain live video.

    def submit(self, frame) -> None:
        if not self.enabled or self.stopped.is_set():
            return
        import cv2

        height, width = frame.shape[:2]
        small = cv2.resize(
            frame, (min(width, 640), max(1, round(height * min(width, 640) / width)))
        )
        with self.condition:
            self.pending = (small, time.monotonic())
            self.condition.notify_all()

    def overlay(self, frame):
        with self.condition:
            detections = (
                list(self.result) if self.enabled and time.monotonic() - self.updated < 1.5 else []
            )
        return annotate(frame, detections)

    def _run(self):
        try:
            while not self.stopped.is_set():
                with self.condition:
                    self.condition.wait_for(
                        lambda: self.pending is not None or self.stopped.is_set()
                    )
                    if self.stopped.is_set():
                        break
                    assert self.pending is not None
                    frame, captured = self.pending
                    self.pending = None
                started = time.monotonic()
                assert self.detector is not None
                detections = self.detector.detect(frame)
                elapsed = time.monotonic() - started
                with self.condition:
                    if not self.stopped.is_set():
                        self.result, self.updated = detections, captured
                        self.inference_ms = round(elapsed * 1000, 1)
                if not self.stopped.is_set() and self.on_result is not None:
                    self.on_result(detections, captured)
                self.stopped.wait(max(0, 1 / self.config.max_fps - elapsed))
        except Exception as exc:
            with self.condition:
                self.error, self.enabled, self.result = str(exc), False, []
            if self.on_error is not None:
                self.on_error(str(exc))

    def pause(self):
        self.stopped.set()
        with self.condition:
            self.pending = None
            self.result = []
            self.updated = 0
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=10)
        self.inference_ms = None

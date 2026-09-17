"""OpenCV DNN object detection, and each camera's client of the shared scheduler."""

import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from sentry_mode.config import DetectionConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.sources.models import PRIMARY_CAMERA
from sentry_mode.vision.labels import CLASSES

if TYPE_CHECKING:
    from sentry_mode.vision.scheduler import InferenceScheduler


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
    """One camera's view of the shared model: its demand, its latest boxes, its errors.

    The worker no longer owns a thread or a network. It hands frames to the scheduler and
    keeps what comes back for this camera, in this epoch, at this camera's confidence.
    A worker made without a scheduler gets one of its own, which is what a single camera
    always had.
    """

    def __init__(
        self,
        config: DetectionConfig,
        scheduler: "InferenceScheduler | None" = None,
        source_id: str = PRIMARY_CAMERA,
    ):
        from sentry_mode.vision.scheduler import InferenceScheduler

        self.config = config
        self.enabled = config.enabled
        self.scheduler = scheduler if scheduler is not None else InferenceScheduler(config)
        self.source_id = source_id
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.stopped.set()
        self.epoch = 0
        self.sequence = 0
        self.result: list[Detection] = []
        self.updated = 0.0
        self.inference_ms: float | None = None
        self.error: str | None = None
        self.on_result: Callable[[list[Detection], float], None] | None = None
        self.on_error: Callable[[str], None] | None = None

    @property
    def detector(self):
        return self.scheduler.detector

    @property
    def thread(self) -> threading.Thread | None:
        return self.scheduler.thread

    def status(self) -> dict:
        with self.condition:
            fresh = time.monotonic() - self.updated < 1.5
            state = {
                "enabled": self.enabled,
                "error": self.error,
                "available": self.config.model.is_file(),
                "model": "YOLOX Nano",
                "max_fps": self.config.max_fps,
                "inference_ms": self.inference_ms,
                "objects": [asdict(d) for d in self.result] if fresh and self.enabled else [],
            }
        shared = self.scheduler.status(self.source_id)
        state["requested_fps"] = shared["requested_fps"]
        state["effective_fps"] = shared["effective_fps"]
        return state

    def configure(self, enabled: bool, running: bool) -> None:
        if not enabled:
            self.enabled = False
            self.pause()
            self.error = None
            return
        self.scheduler.load()
        self.enabled = True
        self.error = None
        if running:
            self.start()

    def start(self) -> None:
        if not self.enabled or not self.stopped.is_set():
            return
        try:
            with self.condition:
                self.epoch += 1
                self.sequence = 0
                epoch = self.epoch
                self.stopped.clear()
            self.scheduler.demand(
                self.source_id,
                self,
                fps=self.config.max_fps,
                confidence=self.config.confidence,
                epoch=epoch,
                on_result=self._deliver,
                on_error=self._fail,
            )
        except Exception as exc:
            self.stopped.set()
            self.error = str(exc)
            self.enabled = False  # A detector failure must not stop plain live video.

    def boost(self, factor: float, seconds: float) -> None:
        self.scheduler.boost(self.source_id, factor, seconds)

    def submit(self, frame) -> None:
        if not self.enabled or self.stopped.is_set():
            return
        import cv2

        from sentry_mode.vision.scheduler import FramePacket

        height, width = frame.shape[:2]
        small = cv2.resize(
            frame, (min(width, 640), max(1, round(height * min(width, 640) / width)))
        )
        with self.condition:
            self.sequence += 1
            packet = FramePacket(self.source_id, self.epoch, self.sequence, time.monotonic(), small)
        self.scheduler.offer(packet)

    def overlay(self, frame):
        with self.condition:
            detections = (
                list(self.result) if self.enabled and time.monotonic() - self.updated < 1.5 else []
            )
        return annotate(frame, detections)

    def _deliver(self, inference) -> None:
        packet = inference.packet
        detections = [d for d in inference.detections if d.confidence >= self.config.confidence]
        with self.condition:
            if self.stopped.is_set() or packet.epoch != self.epoch:
                return  # a result for a frame from before the last restart
            self.result, self.updated = detections, packet.captured
            self.inference_ms = round(inference.elapsed * 1000, 1)
        if self.on_result is not None:
            self.on_result(detections, packet.captured)

    def _fail(self, message: str) -> None:
        with self.condition:
            if self.stopped.is_set():
                return
            self.error, self.enabled, self.result = message, False, []
            self.stopped.set()
        if self.on_error is not None:
            self.on_error(message)

    def pause(self):
        with self.condition:
            self.stopped.set()
            self.epoch += 1
            self.result = []
            self.updated = 0
            self.condition.notify_all()
        self.scheduler.release(self.source_id, self)
        self.inference_ms = None

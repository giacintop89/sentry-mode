"""OpenCV is confined to this headless acquisition adapter."""

import glob
import logging
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cv2 import VideoCapture

from sentry_mode.config import CameraConfig
from sentry_mode.core.errors import HardwareError

logger = logging.getLogger(__name__)


def list_cameras() -> list[str]:
    return sorted(glob.glob("/dev/v4l/by-id/*") or glob.glob("/dev/video*"))


class Camera:
    def __init__(self, config: CameraConfig):
        self.config = config
        self._capture: "VideoCapture | None" = None

    def open(self) -> None:
        if not self.config.enabled:
            raise HardwareError("camera is disabled")
        if self._capture is not None:
            return
        import cv2

        try:
            self._capture = cv2.VideoCapture(self.config.device)
            if not self._capture.isOpened():
                raise HardwareError(f"cannot open camera {self.config.device}")
            if self.config.fourcc:
                # Asked for before the size: a UVC device picks its pixel format first, and the
                # uncompressed default is what caps 1080p at 5 fps rather than 30.
                self._capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.config.fourcc))
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            self._capture.set(cv2.CAP_PROP_FPS, self.config.fps)
            # V4L2 passes the mode straight through: 1 is manual, 3 the aperture-priority
            # default. Both branches are set because the device keeps the mode between opens,
            # so leaving one alone would strand it wherever a previous setting left it.
            self._capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1 if self.config.exposure else 3)
            if self.config.exposure:
                self._capture.set(cv2.CAP_PROP_EXPOSURE, self.config.exposure)
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception as exc:
            self.close()
            raise HardwareError(f"camera initialization failed: {exc}") from exc
        logger.info("Camera opened: %s", self.config.device)

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def capture_frame(self):
        if self._capture is None:
            raise HardwareError("camera is not open")
        try:
            ok, frame = self._capture.read()
        except Exception as exc:
            raise HardwareError(f"camera read failed: {exc}") from exc
        if not ok or frame is None or frame.size == 0:
            raise HardwareError("camera returned no frame")
        return frame

    def save_frame(self, frame, output: str) -> None:
        import cv2

        try:
            saved = cv2.imwrite(output, frame)
        except Exception as exc:
            raise HardwareError(f"cannot write image {output}: {exc}") from exc
        if not saved:
            raise HardwareError(f"cannot write image {output}")

    def encode_jpeg(self, frame, max_width: int = 960) -> bytes:
        import cv2

        height, width = frame.shape[:2]
        if width > max_width:
            frame = cv2.resize(frame, (max_width, int(height * max_width / width)))
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise HardwareError("camera frame JPEG encoding failed")
        return encoded.tobytes()

    def info(self) -> dict[str, float]:
        import cv2

        if self._capture is None:
            raise HardwareError("camera is not open")
        negotiated = int(self._capture.get(cv2.CAP_PROP_FOURCC))
        return {
            "width": self._capture.get(cv2.CAP_PROP_FRAME_WIDTH),
            "height": self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "fps": self._capture.get(cv2.CAP_PROP_FPS),
            "fourcc": negotiated.to_bytes(4, "little").decode("ascii", "replace").strip(),
        }

    def measure_latency(self, frames: int = 20) -> dict[str, float | None]:
        """Time the open, the first frame, and the intervals between the frames after it."""
        was_open = self._capture is not None
        started = time.perf_counter()
        self.open()
        opened = time.perf_counter()
        self.capture_frame()
        first = time.perf_counter()
        intervals, previous = [], first
        for _ in range(max(1, frames)):
            self.capture_frame()
            now = time.perf_counter()
            intervals.append((now - previous) * 1000)
            previous = now
        return {
            "open_ms": None if was_open else round((opened - started) * 1000, 1),
            "first_frame_ms": round((first - started) * 1000, 1),
            "frame_interval_ms": round(statistics.median(intervals), 1),
            "slowest_frame_ms": round(max(intervals), 1),
            "effective_fps": round(len(intervals) * 1000 / sum(intervals), 1),
            "frames": len(intervals),
        }

    def is_available(self) -> bool:
        was_open = self._capture is not None
        try:
            self.open()
            self.capture_frame()
            return True
        except (HardwareError, ImportError):
            return False
        finally:
            if not was_open:
                self.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

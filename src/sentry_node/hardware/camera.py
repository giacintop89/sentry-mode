"""OpenCV is confined to this headless acquisition adapter."""

import glob
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cv2 import VideoCapture

from sentry_node.config import CameraConfig
from sentry_node.core.errors import HardwareError

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
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            self._capture.set(cv2.CAP_PROP_FPS, self.config.fps)
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
        return {
            "width": self._capture.get(cv2.CAP_PROP_FRAME_WIDTH),
            "height": self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "fps": self._capture.get(cv2.CAP_PROP_FPS),
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

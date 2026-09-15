from unittest.mock import MagicMock, patch

import pytest

from sentry_mode.config import CameraConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.hardware.camera import Camera
from sentry_mode.vision.capture import capture_image


def test_failed_open_releases_handle():
    with patch("cv2.VideoCapture") as capture:
        capture.return_value.isOpened.return_value = False
        camera = Camera(CameraConfig())
        assert not camera.is_available()
        capture.return_value.release.assert_called_once()


def test_capture_failure_releases_handle(tmp_path):
    with patch("cv2.VideoCapture") as capture:
        capture.return_value.read.return_value = (False, None)
        with pytest.raises(HardwareError):
            capture_image(Camera(CameraConfig()), tmp_path / "frame.jpg")
        capture.return_value.release.assert_called_once()


def test_capture_image_write_failure(tmp_path):
    with patch("cv2.VideoCapture") as capture, patch("cv2.imwrite", return_value=False):
        frame = MagicMock()
        frame.size = 10
        capture.return_value.read.return_value = (True, frame)
        with pytest.raises(HardwareError, match="cannot write"):
            capture_image(Camera(CameraConfig()), tmp_path / "frame.jpg")
        capture.return_value.release.assert_called_once()

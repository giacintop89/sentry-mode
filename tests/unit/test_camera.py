from unittest.mock import MagicMock, call, patch

import cv2
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


def latency_of(camera, ticks, frames):
    clock = MagicMock(perf_counter=MagicMock(side_effect=ticks))
    with patch("sentry_mode.hardware.camera.time", clock):
        return camera.measure_latency(frames=frames)


def test_measure_latency_times_the_open_the_first_frame_and_the_intervals():
    with patch("cv2.VideoCapture") as capture:
        frame = MagicMock()
        frame.size = 10
        capture.return_value.read.return_value = (True, frame)
        camera = Camera(CameraConfig())
        latency = latency_of(camera, [0.0, 0.5, 1.2, 1.4, 1.5, 1.9], frames=3)
    assert latency == {
        "open_ms": 500.0,
        "first_frame_ms": 1200.0,
        "frame_interval_ms": 200.0,
        "slowest_frame_ms": 400.0,
        "effective_fps": 4.3,
        "frames": 3,
    }


def test_measure_latency_cannot_time_an_open_device():
    with patch("cv2.VideoCapture") as capture:
        frame = MagicMock()
        frame.size = 10
        capture.return_value.read.return_value = (True, frame)
        camera = Camera(CameraConfig())
        camera.open()
        latency = latency_of(camera, [0.0, 0.0, 0.1, 0.2], frames=1)
    assert latency["open_ms"] is None
    assert latency["first_frame_ms"] == 100.0
    assert capture.call_count == 1


def test_measure_latency_reports_a_failed_device():
    with patch("cv2.VideoCapture") as capture:
        capture.return_value.read.return_value = (False, None)
        with pytest.raises(HardwareError):
            Camera(CameraConfig()).measure_latency()


def requested_properties(config):
    with patch("cv2.VideoCapture") as capture:
        Camera(config).open()
        return [call.args for call in capture.return_value.set.call_args_list]


def test_open_requests_a_compressed_format_before_the_size():
    requests = requested_properties(CameraConfig())
    assert requests[0] == (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    properties = [property for property, _ in requests]
    assert properties.index(cv2.CAP_PROP_FOURCC) < properties.index(cv2.CAP_PROP_FRAME_WIDTH)


def test_open_keeps_the_device_default_without_a_fourcc():
    properties = [property for property, _ in requested_properties(CameraConfig(fourcc=""))]
    assert cv2.CAP_PROP_FOURCC not in properties
    assert cv2.CAP_PROP_FRAME_WIDTH in properties


def test_info_reports_the_negotiated_format():
    with patch("cv2.VideoCapture") as capture:
        capture.return_value.get.return_value = float(cv2.VideoWriter_fourcc(*"MJPG"))
        camera = Camera(CameraConfig())
        camera.open()
        assert camera.info()["fourcc"] == "MJPG"


def test_a_configured_exposure_opens_the_device_in_manual():
    with patch("cv2.VideoCapture") as capture:
        Camera(CameraConfig(fps=30, exposure=120)).open()
    settings = capture.return_value.set.call_args_list
    assert call(cv2.CAP_PROP_AUTO_EXPOSURE, 1) in settings
    assert call(cv2.CAP_PROP_EXPOSURE, 120) in settings


def test_zero_opens_the_device_in_its_automatic_mode():
    """The mode outlives the handle, so opening has to say so rather than stay silent."""
    with patch("cv2.VideoCapture") as capture:
        Camera(CameraConfig(exposure=0)).open()
    settings = capture.return_value.set.call_args_list
    assert call(cv2.CAP_PROP_AUTO_EXPOSURE, 3) in settings
    assert not [item for item in settings if item.args[0] == cv2.CAP_PROP_EXPOSURE]

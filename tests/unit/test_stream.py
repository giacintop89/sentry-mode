import threading
from unittest.mock import patch

import pytest

from sentry_mode.config import CameraConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.vision.stream import VideoStream


def test_disabled_camera_fails_cleanly():
    lock = threading.Lock()
    stream = VideoStream(CameraConfig(enabled=False), lock)
    with pytest.raises(HardwareError, match="disabled"):
        stream.start()
    stream.stop()
    assert not lock.locked()
    assert not stream.status()["running"]


def test_worker_read_failure_releases_camera():
    lock = threading.Lock()
    stream = VideoStream(CameraConfig(), lock)
    with patch("sentry_mode.vision.stream.Camera") as adapter:
        camera = adapter.return_value.__enter__.return_value
        camera.encode_jpeg.return_value = b"jpeg"
        camera.capture_frame.side_effect = [object()] * 5 + [HardwareError("camera disconnected")]
        stream.start()
        stream.thread.join(timeout=2)
        assert stream.status()["error"] == "camera disconnected"
        assert stream.next_frame(-1) is None
        assert not lock.locked()
        adapter.return_value.__exit__.assert_called_once()


def test_busy_camera_does_not_create_worker():
    lock = threading.Lock()
    stream = VideoStream(CameraConfig(), lock)
    with lock, pytest.raises(BlockingIOError):
        stream.start()
    assert stream.thread is None


def test_sentry_without_preview_never_encodes_and_preview_can_stop_independently():
    import time

    import numpy as np

    lock = threading.Lock()
    stream = VideoStream(CameraConfig(), lock)
    with (
        patch("sentry_mode.vision.stream.Camera") as adapter,
        patch("sentry_mode.vision.detection.ObjectDetector") as detector,
    ):
        camera = adapter.return_value.__enter__.return_value
        camera.capture_frame.return_value = np.zeros((120, 160, 3), np.uint8)
        camera.encode_jpeg.return_value = b"jpeg"
        detector.return_value.detect.return_value = []
        try:
            state = stream.set_sentry(True, 5, 0.7)
            assert state["capture_running"] and not state["running"]
            time.sleep(0.3)
            camera.encode_jpeg.assert_not_called()
            assert state["capture_size"] == (640, 360)
            stream.start()
            stream.add_viewer(1)
            assert stream.next_frame(-1) is not None
            stream.stop()
            stream.add_viewer(-1)
            time.sleep(0.3)
            count = camera.encode_jpeg.call_count
            time.sleep(0.3)
            assert camera.encode_jpeg.call_count == count
            assert stream.status()["capture_running"] and not stream.status()["running"]
            with pytest.raises(BlockingIOError, match="Sentry"):
                stream.set_detection(False)
            stream.set_sentry(False)
            assert not stream.status()["capture_running"] and not lock.locked()
        finally:
            stream.close()

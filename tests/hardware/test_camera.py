import pytest

from sentry_mode.config import load_config
from sentry_mode.hardware.camera import Camera
from sentry_mode.vision.capture import capture_image

pytestmark = pytest.mark.hardware


def test_camera_capture(tmp_path):
    output = tmp_path / "frame.jpg"
    capture_image(Camera(load_config().camera), output)
    assert output.stat().st_size > 0

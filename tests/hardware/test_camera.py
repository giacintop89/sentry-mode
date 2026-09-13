import pytest

from sentry_node.config import load_config
from sentry_node.hardware.camera import Camera
from sentry_node.vision.capture import capture_image

pytestmark = pytest.mark.hardware


def test_camera_capture(tmp_path):
    output = tmp_path / "frame.jpg"
    capture_image(Camera(load_config().camera), output)
    assert output.stat().st_size > 0

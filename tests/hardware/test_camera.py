import pytest

from vision_node.config import load_config
from vision_node.hardware.camera import Camera
from vision_node.vision.capture import capture_image

pytestmark = pytest.mark.hardware


def test_camera_capture(tmp_path):
    output = tmp_path / "frame.jpg"
    capture_image(Camera(load_config().camera), output)
    assert output.stat().st_size > 0

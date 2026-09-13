from unittest.mock import patch

from sentry_node.config import Settings
from sentry_node.hardware.status import inspect_hardware, network_available


def test_status_degrades_independently():
    with (
        patch("sentry_node.hardware.status.Camera") as camera,
        patch("sentry_node.hardware.status.Microphone") as microphone,
        patch("sentry_node.hardware.status.Speaker") as speaker,
        patch("sentry_node.hardware.status.network_available", return_value=False),
    ):
        camera.return_value.is_available.return_value = False
        microphone.return_value.is_available.return_value = True
        speaker.return_value.is_available.return_value = False
        result = inspect_hardware(Settings())
    assert not result.camera_available
    assert result.microphone_available
    assert not result.speaker_available
    assert not result.network_available


def test_network_failure():
    with patch("sentry_node.hardware.status.socket.socket", side_effect=OSError):
        assert not network_available()

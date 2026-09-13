from unittest.mock import patch

from sentry_node.cli import main
from sentry_node.core.errors import HardwareError


def test_config_validate(capsys):
    assert main(["config", "validate"]) == 0
    assert "Configuration valid" in capsys.readouterr().out


def test_failed_camera_test_returns_nonzero():
    with patch("sentry_node.cli.Camera") as camera:
        camera.return_value.__enter__.side_effect = HardwareError("missing camera")
        assert main(["camera", "test"]) == 1

from unittest.mock import patch

from sentry_mode.cli import main
from sentry_mode.core.errors import HardwareError


def test_config_validate(capsys):
    assert main(["config", "validate"]) == 0
    assert "Configuration valid" in capsys.readouterr().out


def test_failed_camera_test_returns_nonzero():
    with patch("sentry_mode.cli.Camera") as camera:
        camera.return_value.measure_latency.side_effect = HardwareError("missing camera")
        assert main(["camera", "test"]) == 1


def test_serve_passes_separate_https_bind_address():
    with patch("sentry_mode.web.serve") as serve:
        assert main(["serve", "--host", "127.0.0.1", "--https-host", "0.0.0.0"]) == 0
    assert serve.call_args.args[1:] == ("127.0.0.1", 8083)
    assert serve.call_args.kwargs["https_host"] == "0.0.0.0"

"""The V1 baseline, recorded before satellites exist.

Every satellite increment has to leave this installation alone: the same camera
device string, the same rules, the same read-only API. These fixtures are the
evidence, so a later migration cannot quietly drop a field.
"""

import importlib.util
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sentry_mode.config import Settings, load_config
from sentry_mode.sentry.engine import Sentry
from sentry_mode.sentry.migration import dump_v1, to_v1

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures/satellites/v1"
SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"

ACTION_TYPES = {
    "audio",
    "photo",
    "sound",
    "ssh",
    "telegram",
    "tts",
    "tune",
    "video",
    "wait",
}


@pytest.fixture
def state(tmp_path):
    """The recorded rules file, loaded the way the node loads its own."""
    path = tmp_path / "sentry.json"
    shutil.copy(FIXTURES / "sentry-v1-state.json", path)
    settings = Settings(
        sentry_state_file=path,
        captures_directory=tmp_path / "captures",
        sounds_directory=tmp_path / "sounds",
    )
    return Sentry(settings, MagicMock(), MagicMock())


@pytest.mark.parametrize(
    "fixture,device",
    [
        ("config-legacy-index-device.yaml", 0),
        (
            "config-legacy-usb-device.yaml",
            "/dev/v4l/by-id/usb-046d_Logitech_StreamCam_0000000E-video-index0",
        ),
        ("config-legacy-url-device.yaml", "http://satellite.invalid:8080/?action=stream"),
    ],
)
def test_legacy_camera_device_is_kept_exactly(fixture, device):
    config = load_config(FIXTURES / fixture)
    assert config.camera.device == device
    assert type(config.camera.device) is type(device)


def test_legacy_url_camera_is_not_a_local_device():
    # legacy-primary may already be a network source; migration must not assume an index.
    config = load_config(FIXTURES / "config-legacy-url-device.yaml")
    assert config.camera.width == 640 and config.camera.fps == 10
    assert config.detection.enabled is True


def test_legacy_rules_load_without_error(state):
    assert state.config_error is None
    assert state.revision == 39
    assert [rule.name for rule in state.config.rules] == ["Person at entrance", "Vehicle at gate"]


def test_legacy_rule_details_survive_loading(state):
    # The engine works on the second version; the first is what the old editor reads.
    entrance, gate = to_v1(state.config).rules
    assert entrance.actions[1].type == "video"
    assert entrance.actions[1].audio is True
    assert entrance.actions[1].with_previous is True
    assert gate.region == (0.1, 0.2, 0.9, 0.95)
    assert gate.actions[-1].command_id in state.config.ssh_commands


def test_legacy_rules_round_trip_without_loss(state):
    saved = dump_v1(to_v1(state.config))
    saved["telegram"]["bot_token"] = state.config.telegram.bot_token.get_secret_value()
    recorded = json.loads((FIXTURES / "sentry-v1-state.json").read_text())
    assert saved == recorded["config"]


def test_legacy_fixture_covers_every_action_type(state):
    used = {action.type for rule in state.config.rules for action in rule.actions}
    assert used == ACTION_TYPES


def matches(recorded, current):
    """Shapes are equal, except that an empty list matches a list of anything.

    A machine without a microphone reports no devices; that is not a contract change.
    """
    if isinstance(recorded, dict) and isinstance(current, dict):
        return recorded.keys() == current.keys() and all(
            matches(recorded[key], current[key]) for key in recorded
        )
    if isinstance(recorded, list) and isinstance(current, list):
        return not recorded or not current or matches(recorded[0], current[0])
    return recorded == current


def load_capture():
    spec = importlib.util.spec_from_file_location("capture", SCRIPTS / "capture_legacy_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_api_shapes_are_unchanged(tmp_path):
    capture = load_capture()
    recorded = json.loads((FIXTURES / "api-shapes.json").read_text())
    current = capture.capture(tmp_path)
    assert recorded.keys() == current.keys()
    for path in recorded:
        assert matches(recorded[path], current[path]), f"{path} changed shape"

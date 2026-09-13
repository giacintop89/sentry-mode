import pytest
from pydantic import ValidationError

from vision_node.config import load_config


def test_defaults(monkeypatch):
    monkeypatch.delenv("VISION_NODE_CONFIG", raising=False)
    assert load_config().camera.width == 1920


def test_environment_overrides_yaml(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("camera:\n  width: 640\n  device: /dev/video2\nlogging:\n  level: INFO\n")
    monkeypatch.setenv("VISION_NODE_CAMERA__WIDTH", "1280")
    monkeypatch.setenv("VISION_NODE_CAMERA__DEVICE", "2")
    monkeypatch.setenv("VISION_NODE_LOGGING__LEVEL", "DEBUG")
    settings = load_config(path)
    assert settings.camera.width == 1280
    assert settings.camera.device == 2
    assert settings.logging.level == "DEBUG"


@pytest.mark.parametrize(
    "text", ["camera:\n  width: -1", "speaker:\n  volume: 101", "unknown: true", "[]"]
)
def test_invalid_configuration(tmp_path, text):
    path = tmp_path / "invalid.yaml"
    path.write_text(text)
    with pytest.raises((ValueError, ValidationError)):
        load_config(path)


def test_config_env_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("node:\n  name: test-node\n")
    monkeypatch.setenv("VISION_NODE_CONFIG", str(path))
    assert load_config().node.name == "test-node"

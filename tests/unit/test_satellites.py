"""The satellite section exists, does nothing, and costs the node nothing."""

import subprocess
import sys
from pathlib import Path

import yaml

import sentry_mode
from sentry_mode.config import Settings, load_config

SOURCE = Path(sentry_mode.__file__).resolve().parents[1]


def imported_modules(module: str) -> set[str]:
    """The modules a fresh interpreter ends up with after importing one of ours."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}, sys; print('\\n'.join(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(SOURCE), "PATH": "/usr/bin:/bin"},
    )
    return set(result.stdout.split())


def test_a_node_that_was_never_told_about_satellites_has_none():
    settings = Settings()
    assert settings.satellites.enabled is False


def test_a_configuration_file_written_before_any_of_this_still_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"node": {"name": "sentry-mode"}}), encoding="utf-8")
    assert load_config(path).satellites.enabled is False


def test_a_satellite_can_be_switched_on_from_the_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"satellites": {"enabled": True}}), encoding="utf-8")
    assert load_config(path).satellites.enabled is True


def test_naming_a_source_does_not_start_the_camera():
    imported = imported_modules("sentry_mode.sources.registry")
    assert "cv2" not in imported
    assert not {name for name in imported if name.startswith("sentry_mode.vision")}


def test_the_wire_format_does_not_drag_in_the_rest_of_the_node():
    imported = imported_modules("sentry_mode.satellites.protocol")
    assert "cv2" not in imported
    assert not {name for name in imported if name.startswith("sentry_mode.sentry")}

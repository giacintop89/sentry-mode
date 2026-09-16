#!/usr/bin/env python3
"""Record the shape of the V1 read-only API, before satellites exist.

The fixture keeps key names and value types, never values: device serials and
tokens stay out of the repository, and a captured shape does not drift when a
capture count or a port changes. Regenerate with:

    python scripts/capture_legacy_api.py

/api/status is not captured: it opens the camera, and a node that is streaming
must not have its device taken away by a fixture refresh.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sentry_mode.config import Settings  # noqa: E402
from sentry_mode.web import NodeControls  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/satellites/v1/api-shapes.json"

PATHS = [
    "/api/config",
    "/api/runtime",
    "/api/streams",
    "/api/video/status",
    "/api/sentry/status",
    "/api/sentry/config",
    "/api/talk/config",
    "/api/soundboard",
    "/api/sounds",
    "/api/captures",
    "/api/camera/list",
    "/api/audio/list",
    "/api/hardware",
]


def shape(value):
    """A value's structure: mappings keep their keys, sequences keep one element."""
    if isinstance(value, dict):
        return {key: shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def capture(directory: Path) -> dict:
    controls = NodeControls(
        Settings(
            sentry_state_file=directory / "sentry.json",
            soundboard_file=directory / "soundboard.json",
            soundboard_directory=directory / "soundboard",
            captures_directory=directory / "captures",
            sounds_directory=directory / "sounds",
        )
    )
    try:
        return {path: shape(controls.get(path)) for path in PATHS}
    finally:
        controls.sentry.disarm()
        controls.video.close()
        controls.stop_runtime()


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        shapes = capture(Path(directory))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(shapes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)} with {len(shapes)} endpoints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

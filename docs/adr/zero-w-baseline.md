# Baseline before the satellite work

**Status:** recorded — 16 September 2026
**Commit:** `5ccc285128bd266a676354cd5fca1e28150ff708`
**Gate:** PR-00 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)

The number to compare against later. Every satellite increment has to leave this
installation working exactly as it is today, so the state of the tree is written down
before the first line of new code.

## Regression run

| Check | Result |
|---|---|
| `pytest` | 224 passed, 3 deselected (the `hardware` marker), 47.7 s |
| of which predate this increment | 215; the other nine are the baseline guard added here |
| `ruff check .` | clean |
| Pre-existing failures | none |

Nothing is skipped to make this pass. A later run that deselects more than three tests, or
that turns a failure into a skip, is a regression regardless of the summary line.

Environment of the run: Raspberry Pi 5, Debian 13 (trixie), kernel 6.18.34+rpt-rpi-2712,
Python 3.13.5, OpenCV 5.0.0, Pydantic 2.13.5. This is the hub, not the satellite target.

`models/` is gitignored, and `speech.kokoro_directory` is a relative path, so
`test_speech.py` takes a different branch in a checkout that has never run
`scripts/setup_speech.sh`. Run the suite from a tree that has the models, or the failure
looks like a regression it is not.

## Drift from the commit the plans analysed

Both plans were written against `4febced0`; this baseline sits on `5ccc285`, which added
pixel format and exposure control, camera reopening, capture settings on the hardware page
and a push-to-talk gain. Python modules did change this time, so the plans' observations
were re-checked rather than assumed:

| Plan observation | Still true on `5ccc285` |
|---|---|
| `Settings.camera` is singular and `device` accepts `int` or `str` | Yes, plus `fourcc` and `exposure` |
| `hardware/camera.py` opens through `cv2.VideoCapture(device)` | Yes |
| Every `VideoStream` builds its own `DetectionWorker` | Yes |
| `sentry/config.py` rules require a visual `object` | Yes, untouched |
| `sentry/engine.py` arming always drives `video.set_sentry` | Yes, untouched |
| `web.py` `NodeControls` owns one video and one monitor | Yes |

The new `camera.fourcc` (default `MJPG`) and `camera.exposure` are UVC negotiations. They
mean nothing to a network source, which is a migration constraint rather than a conflict:
see [the video profile](satellite-video-profile.md).

## Recorded fixtures

In `tests/fixtures/satellites/v1/`, guarded by `tests/unit/test_legacy_baseline.py`:

| Fixture | What it pins |
|---|---|
| `config-legacy-index-device.yaml` | The shipped example shape, camera by index |
| `config-legacy-usb-device.yaml` | The deployed shape, camera by `/dev/v4l/by-id` path |
| `config-legacy-url-device.yaml` | `camera.device` already holding a network URL |
| `sentry-v1-state.json` | Two rules covering all nine action types, a region, a `with_previous` group, an SSH command and `revision` |
| `api-shapes.json` | The key names and value types of thirteen read-only endpoints |

The device strings and identifiers in the fixtures are examples. No serial, token or key
from the running node is recorded.

`api-shapes.json` is regenerated with `python scripts/capture_legacy_api.py`. It records
structure, never values, so refreshing it cannot leak a device serial and a changed capture
count cannot fail a test. `/api/status` is deliberately absent: it opens the camera, and a
fixture refresh must not take the device away from a node that is streaming.

It has already earned its place: moving this increment from `99d1f50` to `5ccc285` failed
the shape test on `/api/config`, which had gained `camera.exposure` and `camera.fourcc`.
That is the intended use — the guard reports the change, a person decides it was meant, and
the fixture is re-recorded against the new baseline.

## What this baseline does not cover

The satellite target is measured separately, in [the runtime ADR](zero-w-runtime.md). The
video pipeline is not measured at all yet: [the video profile](satellite-video-profile.md)
stays pending until a stream runs end to end.

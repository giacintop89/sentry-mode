# Release checklist

What has to be true before a release of the satellite work goes on a board, and where each
of it is checked. The acceptance criteria are the ones in the
[functional plan](sentry-mode-zero-w-functional-plan.md); the numbers are its numbers.

Run both suites, the formatter, the type checker and the contract check:

    PYTHONPATH=src .venv/bin/python -m pytest tests
    cd satellite && ../.venv/bin/python -m pytest tests
    .venv/bin/ruff format --check src tests satellite && .venv/bin/ruff check src tests satellite
    PYTHONPATH=src .venv/bin/python scripts/generate_contracts.py --check

## What each criterion is checked by

| # | Criterion | Where |
|---|---|---|
| ACC-01 | Installs on an original Zero W without the hub's AI dependencies | `satellite/tests/unit/test_release.py` (staged install, nothing built on the board); the agent itself was measured on the board in [zero-w-runtime](adr/zero-w-runtime.md). **The install script has not yet been run on the Zero W.** |
| ACC-02 | An old configuration with no satellites behaves as before | `tests/unit/test_legacy_baseline.py`, `tests/unit/test_config.py` |
| ACC-03 | A PIR rule arms with the camera disabled and opens no camera | `tests/unit/test_correlation.py`, `tests/unit/test_rules_v2.py` (the planner's `camera` and `detector`) |
| ACC-04 | The same event sent twice is one transition | `tests/unit/test_satellite_ingress.py` (duplicates, sequence marks) |
| ACC-05 | A broker that comes back with retained and historic messages raises nothing | `tests/unit/test_satellite_ingress.py`, `tests/unit/test_satellite_service.py` |
| ACC-06 | A node that loses the network goes unknown, never falsely absent | `tests/unit/test_satellite_sessions.py`, `tests/unit/test_presence.py` |
| ACC-07 | A remote camera that stops reading leaves the dashboard and disarming usable | `tests/unit/test_satellite_video.py` (stall, backlog); **not reproduced on hardware** |
| ACC-08 | Two cameras alternating do not add their detections together | `tests/unit/test_multi_source.py` |
| ACC-09 | A photo asked for by PIR A comes from A while the preview shows B | `tests/unit/test_evidence.py` |
| ACC-10 | A photo or audio source that is not available is declared, never substituted | `tests/unit/test_evidence.py`, `tests/unit/test_rules_v2.py` |
| ACC-11 | A remote video with no microphone named records no local microphone | `tests/unit/test_evidence.py` (`test_the_node_camera_keeps_its_microphone_when_none_is_named`), `tests/unit/test_satellite_audio.py` |
| ACC-12 | A simulated event runs nothing real | `tests/unit/test_sentry.py`, `tests/unit/test_correlation.py` (test mode) |
| ACC-13 | Testing an action by hand does the real thing and says so | `tests/unit/test_web.py` (`/api/sentry/v2/rules/test`) |
| ACC-14 | Disarming during waits and sequences cancels coherently | `tests/unit/test_correlation.py`, `tests/unit/test_sentry.py` |
| ACC-15 | Restarting the hub leaves Sentry disarmed and replays nothing | `tests/unit/test_sentry.py`, `tests/unit/test_rules_v2.py` |
| ACC-16 | A hub that falls while listening ends the microphone session at its lease | `tests/unit/test_satellite_audio.py`, `satellite/tests/unit/test_audio.py`; **not reproduced on hardware** |
| ACC-17 | Late or duplicated audio blocks stay bounded and in order | `tests/unit/test_satellite_audio.py` (reassembly, gaps, slow readers); **not measured on hardware** |
| ACC-18 | A beacon that is no longer seen is absent only after the window | `satellite/tests/unit/test_presence.py` |
| ACC-19 | A broken or offline scanner means unknown, not a confirmed absence | `satellite/tests/unit/test_presence.py`, `tests/unit/test_presence.py` |
| ACC-20 | One node's credentials used for another are refused | `tests/unit/test_satellite_service.py`, `tests/unit/test_satellite_video.py` (the gateway's certificate check) |
| ACC-21 | Revoking a node takes back events, commands, video and audio | `tests/unit/test_release.py` (`test_revoking_a_node_takes_back_all_four_channels_at_once`) |
| ACC-22 | Malformed or oversized payloads are refused with a sanitized log | `tests/unit/test_satellite_service.py`, `tests/unit/test_satellite_ingress.py` |
| ACC-23 | One noisy source does not shut the others out | `tests/unit/test_satellite_service.py` (per-node budget, bounded queue) |
| ACC-24 | A fault behaves as the chosen policy says, visibly | `tests/unit/test_correlation.py` (`fault_policy`) |
| ACC-25 | Migration and restore lose no rules or references | `tests/unit/test_migrate_satellites.py`, `tests/unit/test_rules_v2.py`; on a node, `satellite/tests/unit/test_release.py` (backup and rollback together) |
| ACC-26 | 72 hours of running, with metrics | **Not done.** No long run has been made; memory, file descriptors, storage and queue depth over time are unmeasured. |

## Before a release goes out

- [ ] Both suites pass, the formatter and linter are clean, the contracts are up to date.
- [ ] `sentry-satellite package` was run from a clean checkout, and the release id is written down.
- [ ] The release installs on a board that already has one, and `sentry-satellite verify` is clean afterwards.
- [ ] A backup was taken before the update, and a rollback to the previous release was tried at least once.
- [ ] `satellites.network` is set, and the hub logs nothing about the dashboard at startup.
- [ ] Revoking a test node was tried with a video and a listener open, and both ended.
- [ ] The node's `doctor` output was read on the board: no blocking line.

## What this release does not claim

- **No 72-hour run** (ACC-26), and no measurement of growth over time.
- **The hardware gates are partial.** The camera and audio profiles were exercised against
  real hardware only in part, and the presence scanner was proved against real BlueZ but
  not lived with; see the *Not done* section of each ADR.
- **Wi-Fi presence is not supported**: the hub has the shape of a provider and no adapter
  ([presence](adr/satellite-presence.md)).
- **No hub-side release packaging.** The hub is installed from its checkout as before
  ([Raspberry Pi setup](raspberry-pi-setup.md)); only the satellite has releases.

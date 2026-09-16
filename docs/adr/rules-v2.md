# Rules, second version

Status: accepted, PR-05.

## Decision

- **The engine works in the second version only.** A first-version document is
  converted when it is read. The first version survives only as a projection, used
  by the old API and when saving a file that has not been migrated.
- **Identifiers are permanent.** A rule's `id` comes from its name the first time and
  never changes after that. A first-version save keeps each rule's identifier by
  matching names. Engine state, log entries and action contexts are keyed by `id`.
- **No silent loss through the old API.** If a rule cannot be expressed in the first
  version, the old endpoints answer `409 schema_upgrade_required` with the reasons.
  They never return a partial list that a save would then overwrite.
- **The file keeps its version.** An existing first-version file is converted only by
  `scripts/migrate_satellites.py --apply`, which first writes a private backup and a
  checksum manifest, and which `--restore` can undo. A node with no file writes the
  second version when its rules need it. The previous release can therefore always read
  a file it wrote.
- **Plan before start.** `ResourcePlanner` derives the detector, the camera and the
  satellite sources from the enabled rules. Arming refuses any named problem before
  anything is started. A sensor rule does not start video. A sensor rule with a photo
  action runs the camera without the detector (`VideoStream.set_sentry(detect=False)`).
- **Fault scope is part of the rules.** `fault_policy` lives in the rules document, not
  in `satellites`. A converted first-version document is `global`, which is what it has
  always done. A new document is `isolated`: only the rules that depend on the failed
  source are paused.
- **Context on every step.** A queued step carries `ActionContext`: rule id, revision,
  `arm_epoch`, zone, origin and sources. Arming and disarming each advance the epoch, and
  a step from an older epoch is dropped. A sequence is queued in full or not at all.
- **Not everything is armable yet.** `audio_event`, `presence_state` and `health_event`
  pass validation and can be saved, but arming refuses them. Satellite cameras and
  microphones can be named in actions, but using one is refused until the media
  increments add them.

## Why

The plan in `sentry-mode-zero-w-implementation-plan.md` shows `fault_policy` under
`satellites`. It was moved into the rules document for two reasons. First, the policy
decides what happens to rules, including rules that involve no satellite at all. Second,
the "migrated documents stay global" default only makes sense attached to the document
that was migrated.

Keeping first-version files in the first version costs one extra step, the migration
script, before sensor rules can be added to an installation that already has saved
rules. In exchange, the upgrade can be rolled back by installing the previous release,
with no file surgery needed.

## Verified by

`tests/unit/test_rules_v2.py`, `tests/unit/test_migrate_satellites.py`, the V2 cases in
`tests/unit/test_web.py`, and the unchanged V1 baseline in
`tests/unit/test_legacy_baseline.py`, which now reads through the projection.

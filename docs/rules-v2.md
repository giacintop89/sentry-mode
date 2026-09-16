# Rules, second version

The first version of a rule could only say one thing: an object seen by this node's
camera. The second version names what sets the rule off, and which camera or microphone
each action uses. A rule can now start from a satellite sensor, with no camera involved.
The existing editor and its API keep working unchanged for as long as the rules still
fit the first version.

## A rule

```json
{
  "id": "pir-entrance",
  "name": "Someone at the door",
  "enabled": true,
  "trigger": {"type": "sensor_event", "source_id": "zero-entrance.pir",
              "kind": "motion.pir", "edge": "rising"},
  "cooldown_seconds": 30,
  "actions": [
    {"type": "photo", "source_id": "legacy-primary"},
    {"type": "tts", "text": "Hello."}
  ]
}
```

- `id` is permanent. It is lowercase letters, digits and hyphens, at most 64
  characters. Renaming a rule changes `name` and leaves `id` alone. The engine's state,
  the event log and the context of every queued action are keyed by `id`.
- `trigger` is one of the types below. Only `vision`, `sensor_event` and `threshold` can
  be armed today. The others can be saved, so an editor can prepare them, but arming
  refuses them and says why.
- `photo` and `video` actions take `source_id`, the camera to use (`legacy-primary` by
  default). `video` and `audio` actions take `audio_source_id`, the microphone
  (`legacy-microphone` by default). A satellite camera or microphone is accepted in the
  document, but it cannot be used until the media increments arrive.

| Trigger | Fires when | Fields |
|---|---|---|
| `vision` | an object is confirmed on consecutive frames | the first version's fields: `object`, `min_confidence`, `min_count`, `consecutive_detections`, `rearm_after_absence_seconds`, `region`, plus `source_id` |
| `sensor_event` | a sensor reports a change in the given direction | `source_id`, `kind` (e.g. `motion.pir`), `edge` = `rising`, `falling` or `any` |
| `threshold` | a measurement stays past a limit for `for_seconds` | `source_id`, `kind`, exactly one of `above` / `below`, `hysteresis`, `for_seconds` |
| `audio_event` | not armable yet | `source_id`, `kind`, `min_level` |
| `presence_state` | not armable yet | `source_id`, `state` |
| `health_event` | not armable yet | `node_id`, `state` |

### What counts

Only events the satellite door marked eligible reach a rule. A retained message, a
node's opening snapshot, a replay, an out-of-order reading and a reading from an
uncertain clock are all excluded (see [satellites](satellites.md)). The engine checks
two more things against the current arming: the event must have arrived after Sentry
was armed, and no more than `action_ttl_seconds` ago.

A `sensor_event` fires only on a boolean reading of `valid` quality. A degraded reading
is neither edge.

A `threshold` takes its first good reading as the starting point. A value that is
already past the limit when watching starts has not *crossed* it, so the rule waits
until the value comes back and goes past the limit again. Once it fires, it stays quiet
until the value is back by at least `hysteresis`. A missing or doubtful reading is never
taken as zero. It changes nothing, except that a `for_seconds` wait already under way
starts again, because the time spent past the limit is no longer known.

## What arming starts

Before anything is started, the planner works out what the enabled rules need:

- the detector, only if a `vision` rule watches this node's camera;
- the camera, for the detector or because a `photo` or `video` action will need it. If
  only actions need it, the camera runs without the detector, so a photo after a PIR
  starts at once and the model is never loaded;
- nothing at all for a rule that only speaks, plays or messages after a sensor event.

Arming is refused, with a message naming the rule and the source, when a source is
unknown or disabled, or is of the wrong kind; when a satellite source is used while
satellites are switched off; or when a trigger or source type is not available yet.
`GET /api/sentry/v2/config` returns the same plan, so the editor can show the problems
before anyone presses Start.

## When a source fails

`fault_policy` in the rules document decides what happens next:

- `global`: any failure disarms everything. This is what the first version always did,
  and it is the policy a converted first-version file gets.
- `isolated` (the default for new documents): only the rules that depend on the failed
  source are paused, and the log says so. Sentry disarms only when no rule is left.
  If the camera fails and the remaining rules do not need it, it is released.

## Actions carry their context

Each queued step carries the rule's `id`, the revision of the rules it was decided
under, the arming it belongs to (`arm_epoch`), the zone and the event that caused it,
and the sources its actions use. Arming and disarming each move the epoch on, so a step
left over from an earlier arming is dropped rather than run late. A rule's steps are
queued all together or not at all: if the queue lacks room for the whole sequence,
none of it is queued and the log says *Action queue is full; the whole sequence was
skipped*.

## The API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/sentry/v2/config` | — | `schema_version`, `stored_schema_version`, `config` (token blanked), `revision`, `plan`, `sources` |
| POST | `/api/sentry/v2/config` | `{"config", "revision", "clear_telegram_token"}` | as GET |
| POST | `/api/sentry/v2/rules/test` | a rule | runs its actions now, like the first-version test |
| POST | `/api/events/simulate` | `{"rule", "samples": [...]}` (up to 500) | per sample: `fired`, the notes, the threshold phase, and what `would_run` |

The POSTs need the same `X-Sentry-Mode-Control: 1` header and same-origin request as the
other controls. A simulation runs on a state of its own and executes nothing. A sample
is `{"at", "detections": [{"label", "confidence", "box"}]}` for a vision rule, and
`{"at", "value", "quality"}` for a sensor rule.

The first-version endpoints keep their shapes. While the rules contain anything the
first version cannot express, `GET` and `POST /api/sentry/config` answer `409` with
`{"error", "code": "schema_upgrade_required", "reasons": [...]}`. An old editor
therefore cannot load a partial copy and save it back over the new rules.
`/api/sentry/status` keeps its shape for either version.

## The file and the migration

The node never rewrites a first-version file in the second version by itself, because
the previous release may still need to read it. It keeps saving it in the first version
for as long as the rules fit. Saving a rule that does not fit is refused, with the advice
to run the migration. A node with no rules file yet writes the second version as soon
as its rules need it.

```bash
python scripts/migrate_satellites.py --config config/sentry-mode.yaml            # dry run
python scripts/migrate_satellites.py --config config/sentry-mode.yaml --apply
python scripts/migrate_satellites.py --config config/sentry-mode.yaml --restore .local/backups/sentry-….json
```

Stop the node first. A running node keeps the rules it loaded and would write them back
on its next save.

- The dry run prints the rules with the identifiers they will get, and never the
  Telegram token. It writes nothing and opens no device or broker.
- `--apply` copies the file into `backups/` beside it (a `0700` directory, `0600` files),
  together with a manifest that records the path and SHA-256 of the copy. It then
  replaces the file atomically and reads the result back to confirm it matches. A file
  already in the second version is left untouched, so the command is safe to repeat.
- `--restore` checks the backup against its manifest, and refuses a copy that was taken
  from a different rules file. Before restoring, it backs up the current file.

A converted rule gets an identifier made from its name: `Person at entrance` becomes
`person-at-entrance`, and a second rule with the same identifier gets a `-2` suffix.
Saving from the first-version editor later keeps each rule's identifier, matched by
name, and keeps the fault policy.

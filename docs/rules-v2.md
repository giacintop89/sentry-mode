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
- `trigger` is one of the types below. Only `vision`, `sensor_event`, `threshold`,
  `sequence`, `audio_event` and `presence_state` can be armed today. The others can be saved, so an editor can prepare them, but arming
  refuses them and says why.
- `photo` and `video` actions take `source_id`, the camera to use (`legacy-primary` by
  default). See [where the evidence comes from](#where-the-evidence-comes-from).
- `audio` actions take `audio_source_id`, the microphone (`legacy-microphone` by
  default) or a satellite microphone such as `zero-entrance.mic-1`. `video` actions take it
  too, and choose sound as described below. Arming refuses a satellite microphone the hub
  does not know.

| Trigger | Fires when | Fields |
|---|---|---|
| `vision` | an object is confirmed on consecutive frames | the first version's fields: `object`, `min_confidence`, `min_count`, `consecutive_detections`, `rearm_after_absence_seconds`, `region`, plus `source_id` |
| `sensor_event` | a sensor reports a change in the given direction | `source_id`, `kind` (e.g. `motion.pir`), `edge` = `rising`, `falling` or `any` |
| `threshold` | a measurement stays past a limit for `for_seconds` | `source_id`, `kind`, exactly one of `above` / `below`, `hysteresis`, `for_seconds` |
| `sequence` | a sensor event, then a camera confirming it within the window | `steps` (a `sensor_event`, then a `vision`), `within_seconds` (1–60, default 5), `time_basis`, `same_zone` |
| `audio_event` | a satellite microphone reports sound | `source_id`, `kind` (`audio.activity`); `min_level` is refused |
| `presence_state` | a device the satellites watch for arrives or leaves | `source_id`, `state` = `present` or `absent`, `kind` (`presence.state`), `observers` (up to seven other sources watching the same device) |
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

## Where the evidence comes from

A photo or video comes from the camera its step names, and from no other.

- `source_id` is any camera this hub drives: `legacy-primary`, or a satellite camera such
  as `zero-entrance.camera-1`. A rule set off by a camera may say `"trigger_source"`
  instead, and gets the camera that fired. Any other rule has to name its camera; arming
  refuses `trigger_source` after a sensor, because there is no camera to inherit.
- The camera is worked out when the rule fires, and travels with the queued steps.
  Editing the rule, or pointing the dashboard at another camera, changes nothing for
  steps already queued.
- When the rule fires, the hub keeps the newest frame of each camera the first photos
  use, if it is no older than two seconds. The first photo is that frame, marked
  `at_trigger`. Later photos in a series, and photos in later steps, are taken when they
  run, marked `after_trigger`.
- If the camera has nothing newer than two seconds, even after waiting up to three, the
  step does what its `if_unavailable` says:

  | `if_unavailable` | What happens |
  |---|---|
  | `fail` (default) | the log says *…; no other camera was used.* and the sequence goes on |
  | `skip` | the log says *…; step skipped.* and the sequence goes on |
  | `stop` | the log says *…; the rest of this sequence was dropped.* and the later steps of this trigger do not run |

A video records sound only from a microphone the rule chose:

| `audio` | `audio_source_id` | Sound |
|---|---|---|
| `false` | anything | none |
| `true` | a microphone | that microphone |
| `true` | left out, camera `legacy-primary` | `legacy-microphone`, as the first version always did |
| `true` | left out, any other camera | none. The arming notes say so, and so does the log when the video is saved |

A video from a satellite camera therefore never records this hub's microphone unless the
rule asks for it. The editor shows the choice as a **Sound** list, with *No sound* first.

Every capture has a sidecar that records its source, the rule and trigger, and how old
the picture was ([captures](captures.md#where-a-capture-came-from)).

A recording from a satellite microphone is timed by when its sound reached the hub, and
its sidecar says `sound_alignment: "hub_arrival"`.

## Sound on a satellite

An `audio_event` rule fires when a satellite microphone with `activity` switched on
reports `audio.activity` as `true`; the `false` that follows only ends the episode. The
node's own threshold decides what counts as loud, so `min_level` is refused when arming,
and so is this hub's own microphone, which reports no events.

```json
{"type": "audio_event", "source_id": "zero-hall.mic-1", "kind": "audio.activity"}
```

While this hub is playing a sound, and for two seconds after it stops, such an event is
logged as *…heard sound while this hub was playing its own; nothing was done.* and the
rule does not run, so a tune or a spoken warning cannot set off the rule that played it.

## A device arriving or leaving

A `presence_state` rule fires when a device a satellite watches for over Bluetooth
arrives, or when it leaves. It is a device, not a person: a rule may greet it, light
something or send a message, but nothing disarms Sentry on a presence, and this hub, which
scans for nothing itself, is refused as the source.

```json
{"type": "presence_state", "source_id": "zero-hall.tag", "state": "present",
 "observers": ["zero-garage.tag"]}
```

`observers` names other sources watching the same device, on other nodes. The hub puts
what they say together the only way that is safe:

- **present** as soon as one of them sees it, wherever it is;
- **absent** only once every source named in the rule says so;
- **unknown** otherwise — a source that has said nothing yet, or whose scanner cannot see,
  holds the answer there.

A rule fires on the change into the state it asks for, so a device that stays is not an
arrival every time a node repeats it, and a device seen in the hall and then in the garage
never leaves. Unknown sets nothing off, and it is where every rule starts: arming a rule on
a device already at home does not fire it. `state: "absent"` with several observers is the
one to think about twice — it waits for all of them, which is what you want for a door,
and is slower than one node alone.

What the node itself decides — how many sightings make a presence, how long without one
makes an absence, and when it gives up and says unknown — is in
[presence](satellites.md#presence).

## A sensor confirmed by a camera

A `sequence` is the one correlation there is: a sensor event, then a camera detection.
It is two fixed steps, not a language.

```json
{
  "id": "entrance-confirmed",
  "name": "Entrance confirmed",
  "trigger": {
    "type": "sequence", "within_seconds": 5, "time_basis": "hub_observation",
    "same_zone": true,
    "steps": [
      {"type": "sensor_event", "source_id": "zero-entrance.pir", "kind": "motion.pir"},
      {"type": "vision", "source_id": "zero-entrance.camera-1", "object": "person",
       "min_confidence": 0.7, "consecutive_detections": 2}
    ]
  },
  "cooldown_seconds": 60,
  "actions": [
    {"type": "photo", "source_id": "trigger_source"},
    {"type": "telegram", "text": "Movement confirmed by a person at the entrance."}
  ]
}
```

- **The window.** A valid sensor event in the step's direction opens a wait of
  `within_seconds`, from the moment the hub received the event. The log says
  *waiting*. If the camera does not confirm in time, the log says *No person on … within
  5 s of …; nothing was done.*
- **One wait at a time.** Each rule has at most one wait. The sensor firing again while
  a wait is open does not move its deadline; otherwise a PIR that keeps firing would
  keep the window open for as long as somebody walks past. Once the wait has run out,
  the next event opens a new one. An event the hub received before the latest wait
  opened arrived late, and is ignored.
- **What confirms.** The camera step works like a `vision` trigger, but only samples the
  hub received after the event count, and they have to reach
  `consecutive_detections` before the window closes. A person already in view counts
  only through frames taken after the event.
- **Used once.** Confirming closes the wait and fires the rule, subject to its cooldown.
  A confirmation during the cooldown still closes the wait, and the log says so. The
  camera step then stays latched until the object has really been out of sight for
  `rearm_after_absence_seconds`; a sensor event meanwhile is logged as *skipped*.
- **What drops a wait.** A sensor reading of doubtful quality, a gap in the camera's
  samples, or the rule being paused by a fault. Frames that never arrived are not an
  absence, so a gap never releases the latch either. A wait that has been dropped or
  has run out is never confirmed later, and nothing is carried over when Sentry is
  armed again.
- **Zones.** With `same_zone`, arming requires the sensor and the camera to have the same
  zone in the registry, and says which zones they have if not.
- **Time.** `time_basis: hub_observation` means the times are when the hub received the
  event and the frames. That is what the hub can vouch for, not a promise that the
  camera saw the person within five seconds of the motion. `time_basis: capture` would
  compare the moments things happened; no camera can prove when a frame was taken yet,
  so a rule asking for it is saved but does not arm. Readings from a node whose clock is
  uncertain never reach a rule in the first place.
- **Actions.** `trigger_source` in a photo or video is the confirming camera. The
  actions' context records both the sensor event and the camera, and the zone of the
  event. A Telegram step sends its text; it does not attach the photo.

The editor offers this as *Sensor, then camera*.

## What arming starts

Before anything is started, the planner works out what the enabled rules need:

- the detector, only if a `vision` trigger or a sequence's camera step watches this
  node's camera;
- the camera, for the detector or because a `photo` or `video` action will need it. If
  only actions need it, the camera runs without the detector, so a photo after a PIR
  starts at once and the model is never loaded;
- every other camera a `photo` or `video` action uses, streaming without detection for as
  long as Sentry is armed, so its first photo shows the moment the rule fired. The plan
  lists these as `ready`. A camera that fails while armed does not pause any rule; the
  step that needed it reports the failure as described above;
- nothing at all for a rule that only speaks, plays or messages after a sensor event.

Arming is refused, with a message naming the rule and the source, when a source is
unknown or disabled, or is of the wrong kind; when a camera is not one this hub drives;
when a satellite source is used while satellites are switched off; or when a trigger or
source type is not available yet.
`GET /api/sentry/v2/config` returns the same plan, so the editor can show the problems
before anyone presses Start.

## When a source fails

`fault_policy` in the rules document decides what happens next:

- `global`: any failure disarms everything. This is what the first version always did,
  and it is the policy a converted first-version file gets.
- `isolated` (the default for new documents): only the rules that depend on the failed
  source are paused, and the log says *Rule paused: …*. Sentry disarms only when no rule
  is left, and the log then says *Every armed rule depends on something that failed.*

What depends on what:

| What failed | Rules paused | What happens to the cameras |
|---|---|---|
| a camera stops sending | every rule that watches it or takes a photo or video from it | it is released |
| the object detector, shared by every camera | every `vision` rule and every `sequence`; sensor rules keep running, including ones that take photos | cameras still used by the remaining rules keep streaming without detection. This node's camera restarts without the detector, briefly |
| a satellite sensor is disabled, or its node is offline | every rule that listens to it | — |

A paused rule stays paused until Sentry is stopped and started again. Its state is
not recovered: a pending wait is dropped, and a sensor that comes back starts from a
fresh reading.

While some rules are paused, Sentry is armed but **degraded**. The Sentry page shows
*degraded* and the number of paused rules on its badge, with the reasons on hover. The
editor's *If a source fails* setting chooses the policy; it is saved when Sentry starts
or a rule is saved. A document converted from the first version keeps `global`; the
policy is never changed for you.

A node is offline once it has said goodbye, or has been silent for
`satellites.health.offline_after_seconds`. A node that is only *stale* pauses nothing,
and neither does the hub losing the broker; in both cases no event arrives, so no rule
fires on old news, and nothing counts the silence as the sensor being idle.

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
| GET | `/api/sentry/v2/status` | — | see below |
| POST | `/api/events/simulate` | `{"rule", "samples": [...]}` (up to 500) | per sample: `fired`, the notes, the threshold phase, and what `would_run` |

The POSTs need the same `X-Sentry-Mode-Control: 1` header and same-origin request as the
other controls. A simulation runs on a state of its own and executes nothing. A sample
is `{"at", "detections": [{"label", "confidence", "box"}]}` for a vision rule, and
`{"at", "value", "quality"}` for a sensor rule. A sequence takes both: a sample with
`detections` is the camera, any other is the sensor, which may also give `zone` and
`camera_zone`.

`GET /api/sentry/v2/status` says how things stand in words the first-version status has
no room for:

```json
{
  "schema_version": 2, "armed": true, "state": "degraded", "fault_policy": "isolated",
  "test_mode": false, "error": null, "revision": 12,
  "plan": {"detector": true, "camera": true, "vision": [], "ready": [], "watched": ["zero-entrance.pir"], "problems": [], "notes": []},
  "rules": [
    {"id": "entrance-confirmed", "name": "Entrance confirmed", "enabled": true,
     "trigger": "sequence", "state": "waiting", "reason": null, "waiting_seconds_left": 3.2},
    {"id": "person", "name": "Person", "enabled": true, "trigger": "vision",
     "state": "paused", "reason": "Object detection stopped.", "waiting_seconds_left": null}
  ]
}
```

- `state` is `disarmed`, `protected` (armed, every rule running), `degraded` (armed, some
  rules paused) or `fault` (stopped by a failure).
- Each rule's `state` is `off` (not armed), `watching`, `waiting` (a sequence waiting for
  its camera), `latched` (fired, and waiting for the object to leave or the value to
  come back), or `paused`, with the `reason`.
- `plan` is what the current arming runs, after any faults; `null` while disarmed.

The first-version endpoints keep their shapes. A photo or video that names another
camera, chooses `skip` or `stop`, or records sound from another microphone counts as
something the first version cannot express. While the rules contain anything the
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

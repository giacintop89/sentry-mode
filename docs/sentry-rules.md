# Sentry rules

A rule watches for a confirmed appearance of one object category and, when it is satisfied,
runs its [action sequence](action-sequencer.md). Rules live at `/sentry`.

A starter person rule is provided: 70% confidence, three consecutive detections, ten seconds
of observed absence before rearming, a 60-second cooldown, and one announcement.

## Conditions

Each rule names an object category from the detector's 80 and sets:

| Field | Range | What it means |
|---|---|---|
| Minimum confidence | 10–99% | A detection below this is ignored. |
| Minimum object count | 1–20 | How many matching objects a frame needs. |
| Consecutive detections | 1–20 | Confirmations in a row before the rule triggers. |
| Absence before rearming | 1–300 s | Observed absence that lets the rule fire again. |
| Cooldown | 0–3600 s | Minimum time between triggers. |
| Region | optional | Percentages of the image; an object's box center must be inside. |

A rule has a name of up to 64 characters, an enabled flag, and a configuration holds up to
32 rules with unique names.

## How a trigger is decided

Only fresh inference results count toward confirmation, and the engine reads completed
results through a callback instead of polling old overlay boxes. A continuously present
object triggers once: the rule latches, and only observed absence for long enough rearms it,
with the cooldown still applying before the next trigger. Missing or stale frames count as
neither confirmation nor absence. Several objects of the same category are scene occupancy,
not identities.

## Arming

**Start Sentry** arms; **Disarm** stops. Arming needs at least one enabled rule and refuses
a configuration whose audio files, saved SSH commands or — when actions will really run —
Telegram credentials are missing. Saving rules is disabled while armed, and revision checks
reject a stale update from another tab. Sentry keeps running when the browser closes, and a
restart always leaves it disarmed. A camera or detector failure disarms it and says so.

Disarming discards queued actions and cancels local playback, SSH and Telegram requests; a
remote command already running on the far machine may continue.

## Test mode and test buttons

**Test mode** holds back what a *detection* would do: a trigger logs `would_run` lines
describing each step instead of running them. It does not hold back what you ask for by
pressing a button — **Test rule** and the per-step test buttons run for real either way, so
a Telegram test needs the credentials a logged-only rule can go without. **Test rule** is
refused while armed, never saves the rule, and reports what ran in the event log.

The usual rehearsal is: check Test mode, save settings, arm, confirm `triggered` and
`would_run` in the event log, then disarm, uncheck Test mode, save and arm again.

## Event log

The **Event log** tab shows the latest 200 events of the current server session; the same
messages go to the server log. Kinds include `detected`, `triggered`, `rearmed`,
`would_run`, `waiting`, `action_started`, `action_finished`, `action_failed`, `expired`,
`skipped`, `cancelled`, `armed`, `disarmed`, `configured`, `tested` and `fault`.

## Where rules are stored

The editor atomically saves `.local/sentry.json` with private file permissions. That file
takes precedence over the `sentry` YAML section, which supplies initial defaults; override
the path with `sentry_state_file` or `SENTRY_NODE_SENTRY_STATE_FILE`. Arming is never
persisted. A damaged saved configuration prevents arming until a valid one is saved.

`detection_fps` (0.5–5) sets how often Sentry samples, and `action_ttl_seconds` (1–120,
15 by default) how long a queued step may wait before it is dropped as stale.

See also: [action sequencer](action-sequencer.md), [object detection](object-detection.md).

# Action sequencer

A rule's actions are an ordered list of steps. Steps run from top to bottom, a step can be
told to start together with the one before it, and a wait step holds the sequence where it
stands.

## Building a sequence

On the rule editor's **Actions** tab, **Add step** appends one of:

| Step | Settings |
|---|---|
| Take a picture | 1–20 pictures, 0.5–60 s apart |
| Record audio | 1–60 seconds |
| Record a video | 1–60 seconds, with or without sound |
| Speak a message | up to 1000 characters, any installed voice, speed, modification |
| Play a tune | tune, 1–5 repeats, volume, −24…+24 semitones |
| Play an audio file | an uploaded file, 1–5 repeats, volume |
| Send a Telegram message | up to 4096 characters, optionally silent |
| Run a saved SSH command | one saved command |
| Wait | 0.1–60 seconds |

↑ ↓ reorder a step, ✕ removes it, and a rule holds up to 16 steps. The same kind may appear
as often as needed, so a rule can speak, wait four seconds and speak again. A rule needs at
least one step that does something besides wait.

Clicking a step's header closes it to a single line that says what it does — the
announcement's text, the tune with its pitch, the seconds a wait holds. A rule of several
steps opens closed; a single step, being the whole rule, opens. A step with something
missing opens itself when you try to save, since a hidden field the browser cannot focus
would otherwise refuse the save with nothing to look at.

## Running together

Ticking **Together with the step above** starts a step at the same moment as the one before
it. Consecutive ticked steps form one group: the group's members start at once, and the
sequence moves to the next group only when all of them have finished.

Two consequences are worth knowing:

- Only one step can use the speaker at a time. Announcements, tunes and audio files in the
  same group still take turns on the shared audio lock.
- Pictures, videos and audio recordings start in the background and their step returns
  immediately. A **Wait** grouped with one is how you hold the sequence for as long as it
  records, because the group ends with its slowest member — the wait.

The first step has nothing above it, so it never carries the flag.

## When a step fails

The failure is logged as `action_failed` and the sequence carries on with the next step.
Nothing is retried automatically.

## Timing and expiry

The action queue holds 16 entries, one per group. A queued group expires if it has waited
longer than `action_ttl_seconds` (15 by default), which also covers an announcement waiting
for a busy push-to-talk or speech operation. A step's budget starts after the waits ahead of
it, so a pause the rule asked for never makes the steps behind it stale.

Disarming cancels a wait immediately and discards whatever is still queued.

## What test mode logs

With test mode on, a trigger logs one `would_run` line per step instead of running it — for
example `TTS: Hello. Please wait here.`, `+ Tune: Doorbell x2 at -5 st`, `Wait: 4 s`. The
leading `+` marks a step that would have started with the one above it.

## In the saved configuration

Each entry of a rule's `actions` list is one step, in the order it runs. A `wait` step has
`seconds`; any step with `"with_previous": true` starts together with the step before it.
Configurations saved before the field existed keep running as the sequence they always were.

See also: [Sentry rules](sentry-rules.md), [tunes and audio files](tunes-and-audio-files.md),
[captures](captures.md), [Telegram](telegram.md), [SSH commands](ssh-commands.md).

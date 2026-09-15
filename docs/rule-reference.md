# Rule reference

Every field of a Sentry rule, with its limits and what it changes at runtime. The
[rules page](sentry-rules.md) explains arming and test mode; the
[action sequencer](action-sequencer.md) explains how a sequence is executed. This page is
the field-by-field table for both tabs of the editor.

A rule has an identity (a unique **Rule name**, up to 64 characters, and **Rule enabled**),
a set of conditions, and an ordered list of 1 to 16 steps. A node holds up to 32 rules.

## Conditions

What has to happen in front of the camera before the steps run.

| Field | Meaning | Range | Default |
|---|---|---|---|
| **Object type** | The detector's label, matched exactly. Only the categories the model knows are accepted; the field lists them as you type. | a supported category | `person` |
| **Minimum confidence (%)** | Ignores detections the model is less sure about than this. Higher means fewer false alarms and more missed appearances. | 10–99 | 70 |
| **Minimum object count** | How many matching objects must be in the **same frame** for that sample to count. | 1–20 | 1 |
| **Consecutive detections** | How many samples **in a row** must reach that count before the rule fires. | 1–20 | 3 |
| **Absence before rearming (seconds)** | After firing, the rule stays latched until the object has been continuously absent this long. | 1–300 | 10 |
| **Cooldown (seconds)** | Minimum time between two firings of this rule, measured from the previous one. | 0–3600 | 60 |
| **Only objects centered in this image region** | When ticked, only objects whose bounding-box **center** falls inside the rectangle count. Left/Top/Right/Bottom are percentages of the image, left < right and top < bottom. | 0–100 each | off |

### How they combine

While armed, the node samples the scene at **Detections/sec** (0.5–5, set in the Monitoring
strip). At each sample, for each enabled rule:

1. Each detection is kept only if its label is the **Object type**, its confidence reaches
   **Minimum confidence**, and — with a region set — its center is inside that region.
2. If fewer than **Minimum object count** survive, the consecutive counter resets to zero
   and the absence clock starts. A latched rule that has now been without its object for
   **Absence before rearming** unlatches and logs `rearmed`.
3. Otherwise the counter goes up by one. The first hit of a new appearance logs `detected`.
4. When the counter reaches **Consecutive detections**, the rule is not latched, and
   **Cooldown** has elapsed since the last firing, the rule fires: it latches, logs
   `triggered`, and hands its steps to the action queue (or logs `would_run` lines in test
   mode).

Confirmation time is roughly *Consecutive detections ÷ Detections/sec*: three samples at
2/s is about 1 to 1.5 seconds after the object appears.

Latch and cooldown are both necessary and they answer different questions. The latch means
one firing per appearance, no matter how long the object stays; the cooldown means a rule
cannot fire in bursts even when the object keeps leaving and returning. With the defaults,
someone who steps out for 12 seconds and comes back 20 seconds after the announcement gets
nothing: the rule rearmed, but the 60-second cooldown had not elapsed.

If more than `max(2 s, 3 ÷ Detections/sec)` passes between samples — a stalled camera, a
detector under load — every rule's counters and absence clocks reset, so a confirmation is
never stitched together from moments far apart.

## Actions

The steps a rule runs when it fires, in editor order. Every step shares
**Together with the step above**: it starts the step at the same moment as the one before
it instead of waiting for it, and consecutive ticked steps form one group the sequence
waits for as a whole. Only one step can hold the speaker at a time, so grouped
announcements, tunes and audio files still take turns. A step that fails is logged as
`action_failed` and the sequence carries on.

| Step | Fields | Range | Default | Behavior |
|---|---|---|---|---|
| **Take a picture** | Number of pictures | 1–20 | 1 | The first is immediate; the series runs in the background, so the next step does not wait for it. |
| | Time between pictures (s) | 0.5–60 | 2 | Shorter than the camera update rate repeats the same frame. |
| **Record audio** | Recording length (s) | 1–60 | 10 | Records the node microphone in the background. |
| **Record a video** | Video duration (s) | 1–60 | 10 | Records in the background. |
| | Record sound with the video | on/off | on | Without a working microphone the video is still saved, silent. |
| **Speak a message** | Announcement | 1–1000 chars | — | Synthesized on the node and played on its speaker. |
| | Voice | any voice installed here | `en` | Steps of one rule may use different voices; a voice that is gone stays in the step as a missing entry. |
| | Voice preset / Custom pitch | natural, demon, chipmunk, custom / −12 to +12 semitones | natural / 0 | Pitch without changing speaking speed. |
| | Volume (%) | 0–100 | 60 | Applies to this step only. |
| | Speech speed (wpm) | 80–450 | 175 | |
| **Play a tune** | Tune | one of the built-in tunes | chime | Generated on the node, no file needed. |
| | Repeat | 1–5 | 1 | |
| | Volume (%) | 0–100 | 60 | |
| | Pitch (semitones) | −24 to +24 | 0 | Moves every note by the same interval; the length is unchanged. |
| **Play an audio file** | Audio file | a file in the node library | — | MP3, WAV, OGG, M4A or FLAC, up to 60 s and 10 MB; any rule can use it. |
| | Repeat | 1–5 | 1 | |
| | Volume (%) | 0–100 | 80 | |
| **Send a Telegram message** | Telegram message | 1–4096 chars | — | Sent as written through the saved bot; needs a token and chat ID. |
| | Send silently | on/off | off | Delivers without a notification sound. |
| **Run a saved SSH command** | Saved command | an ID from Integrations | — | Runs exactly as saved, with its own host, user, port, identity file and 1–60 s timeout. |
| **Wait** | Wait before the next step (s) | 0.1–60 | 2 | Holds the sequence; background pictures, videos and recordings keep running. |

### How a sequence is executed

Firing splits the steps into groups and queues them. The queue runs one group at a time and
takes the next only when every member of the current one has returned, so the order between
groups is exact and concurrency inside a group is explicit. Photos, videos and audio
recordings hand their work to background threads, so the sequence moves on while they
finish; their files appear in the Captures tab.

Each group carries an expiry of `action_ttl_seconds` (default 15) counted from the firing,
shifted by the waits that precede it, so a step late in a long sequence is not discarded as
stale just because the rule asked for a pause. Disarming cancels pending work, discards the
queue and stops local playback; an SSH command already running on the far machine may
continue.

In test mode nothing runs: each step is logged as a `would_run` line instead. The test
buttons in the editor are the opposite — they act for real even in test mode, because they
are what you asked for by pressing a button.

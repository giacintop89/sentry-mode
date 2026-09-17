# Captures

Photos, videos and audio recordings taken by the node, listed in the Sentry **Captures**
tab. A rule's photo or video can come from any camera the hub drives, including a
satellite camera ([rules](rules-v2.md#where-the-evidence-comes-from)).

## What produces one

- A rule's picture, video or audio recording step ([action sequencer](action-sequencer.md)).
- **Record** on the Video view — the live preview as an H.264 MP4 with sound, up to 60 s.
- **Record** on the Voice view — a message from the phone microphone, up to 120 s.

A picture is the newest camera frame as a JPEG; a series takes the first at once and the
rest in the background. An audio recording is sound from the microphone the step names — this node's, or a
satellite's — as an AAC `.m4a`. A rule
video is an H.264 MP4 at up to 1280 pixels wide and 10 frames/second, and while it records
the camera temporarily switches to that size and rate. **Record sound with the video** (on
by default) muxes microphone audio in; when the microphone is unavailable the video is still
saved, silent, and the event log says why. Disarming stops a recording early and keeps what
was captured.

Encoding needs `ffmpeg` with libx264, installed by the Pi bootstrap helper. At most three
videos are encoded at once; a fourth is refused and the log says so.

## Where a capture came from

Next to each new file, a JSON sidecar with the same name records where it came from:

```json
{
  "schema_version": 1, "name": "20260917-101500-250-door.jpg", "kind": "photo",
  "saved_at": "2026-09-17T10:15:00.310+02:00",
  "source_id": "zero-entrance.camera-1", "source_name": "Entrance camera",
  "origin": "satellite", "zone": "entrance",
  "rule_id": "door", "rule_name": "Door", "rule_revision": 7,
  "trigger_id": "…", "trigger_origin": ["<event id>"], "trigger_zone": "entrance",
  "arm_epoch": 3, "triggered_at": "2026-09-17T08:15:00.180+00:00",
  "timing": "at_trigger", "captured_at": "2026-09-17T08:15:00.090+00:00",
  "frame_age_seconds": 0.09, "seconds_after_trigger": -0.09,
  "sequence": {"index": 1, "count": 1},
  "width": 640, "height": 480, "quality": 90
}
```

- `timing` is `at_trigger` for the frame kept when the rule fired, `after_trigger` for a
  picture or clip taken later, and `manual` for **Record** on the Video view.
- A video adds `audio_source_id` (null when silent), `requested_seconds`,
  `recorded_seconds` (less when the camera stopped sending), `sound` and `sound_error`.
- Sound from a satellite microphone adds `sound_alignment: "hub_arrival"`: it is lined up
  by when it reached the hub, not by the node's clock.
- A message from the phone has `origin: "browser"` and no source.

`GET /api/captures` adds an `evidence` object with the main fields to each capture that
has a sidecar. Files saved before sidecars existed are listed and served as before,
without it.

A file is written under a `.part` name and renamed when complete, after its sidecar. A
crash can therefore leave a `.part` file or a sidecar without its file, but never a file
that looks complete and is not. Leftovers older than ten minutes are removed when the node
starts and after each save. Deleting a capture, by hand or because it is past the newest
200, removes its sidecar too.

## Storage

Files are saved in `.local/captures/` — override with `captures_directory` or
`SENTRY_MODE_CAPTURES_DIRECTORY` — and only the newest 200 are kept.

The **Captures** view lists them to view, play or delete. GET `/api/captures` lists them,
`/captures/<name>` serves one with byte ranges so phone video players can seek, and POST
`/api/captures/delete` with `{"name": ...}` removes one. Names are validated: a path outside
the directory is refused.

Test mode logs photos, audio and video as `would_run` and captures nothing.

See also: [camera and live video](camera-and-video.md), [push-to-talk](push-to-talk.md).

# Captures

Photos, videos and audio recordings taken by the node, listed in the Sentry **Captures**
tab.

## What produces one

- A rule's picture, video or audio recording step ([action sequencer](action-sequencer.md)).
- **Record** on the Video view — the live preview as an H.264 MP4 with sound, up to 60 s.
- **Record** on the Voice view — a message from the phone microphone, up to 120 s.

A picture is the newest camera frame as a JPEG; a series takes the first at once and the
rest in the background. An audio recording is node-microphone sound as an AAC `.m4a`. A rule
video is an H.264 MP4 at up to 1280 pixels wide and 10 frames/second, and while it records
the camera temporarily switches to that size and rate. **Record sound with the video** (on
by default) muxes microphone audio in; when the microphone is unavailable the video is still
saved, silent, and the event log says why. Disarming stops a recording early and keeps what
was captured.

Encoding needs `ffmpeg` with libx264, installed by the Pi bootstrap helper.

## Storage

Files are saved in `.local/captures/` — override with `captures_directory` or
`SENTRY_MODE_CAPTURES_DIRECTORY` — and only the newest 200 are kept.

The **Captures** view lists them to view, play or delete. GET `/api/captures` lists them,
`/captures/<name>` serves one with byte ranges so phone video players can seek, and POST
`/api/captures/delete` with `{"name": ...}` removes one. Names are validated: a path outside
the directory is refused.

Test mode logs photos, audio and video as `would_run` and captures nothing.

See also: [camera and live video](camera-and-video.md), [push-to-talk](push-to-talk.md).

# HTTP API

Every endpoint is served by the dashboard process. Responses are JSON unless stated.

## Rules for POST

Every POST requires the same-origin header `X-Sentry-Mode-Control: 1`. These also require
`Content-Type: application/json`: `/api/speech`, `/api/video/detection`,
`/api/sentry/config`, `/api/sentry/rules/test`, `/api/soundboard`, `/api/soundboard/delete`,
`/api/soundboard/play`, `/api/captures/delete`, `/api/hardware`, `/api/sounds/delete`,
`/api/sounds/play`, `/api/tunes/play`. Uploads use `application/octet-stream`. There are no
arbitrary shell or filesystem-path controls, and no login: bind to loopback or use a trusted
local network.

## Node and hardware

| Method and path | Purpose |
|---|---|
| GET `/api/status` | Node status summary (cached for ten seconds). |
| GET `/api/config`, POST `/api/config/validate` | Effective configuration; validation. |
| GET `/api/camera/list`, `/api/audio/list` | Devices the node reports. |
| POST `/api/camera/test`, `/api/camera/capture` | Camera check; JPEG snapshot. |
| POST `/api/audio/test-input`, `/api/audio/test-output` | Microphone and tone tests. |
| GET/POST `/api/hardware` | Selected devices and speaker volume. |
| GET `/api/runtime`, POST `/api/runtime/start`, `/api/runtime/stop` | The idle runtime. |
| GET `/api/streams` | Whether the node is streaming video or audio. |

## Video and audio

| Method and path | Purpose |
|---|---|
| GET `/api/video` | MJPEG stream. |
| POST `/api/video/start`, `/api/video/stop` | Start and stop the camera worker. |
| GET `/api/video/status` | `running`, `capture_running`, `monitoring`, `recording`, detection state and timing. |
| POST `/api/video/detection` | `{"enabled": true|false}`. |
| POST `/api/video/record/start`, `/api/video/record/stop` | Record the preview. |
| GET `/api/audio/monitor` | Raw 16-bit mono PCM at 16 kHz from the node microphone. |

## Speech and push-to-talk

| Method and path | Purpose |
|---|---|
| POST `/api/speech` | `text`, optional `voice`, `rate`, `effects` (`preset`, `pitch`, `volume`). |
| GET `/api/speech/voices` | Voices available on this node. |
| GET/POST `/api/soundboard`, POST `/api/soundboard/play`, `/api/soundboard/delete` | Saved messages. |
| GET `/api/talk/config` | HTTPS setup link settings. |
| POST `/api/talk/start` | Session token, sample rate, duration limit; optional effects body. |
| POST `/api/talk/chunk`, `/api/talk/stop`, `/api/talk/cancel` | Need `X-Sentry-Mode-Talk`; chunks need consecutive `X-Audio-Sequence` from zero, ≤ 9600 bytes. |

Presets are `natural`, `demon`, `chipmunk` and `custom` (with `pitch` in semitones).

## Sentry

| Method and path | Purpose |
|---|---|
| GET `/api/sentry/config` | Rules, SSH commands, Telegram settings (blank `bot_token`), `telegram_token_configured`, categories, revision. |
| POST `/api/sentry/config` | `{ "config": {...}, "revision": N }`, optional `"clear_telegram_token": true`. |
| GET `/api/sentry/status` | Armed/test state, action status, recent events. |
| POST `/api/sentry/start`, `/api/sentry/stop` | Arm and disarm. |
| POST `/api/sentry/rules/test` | Run a draft rule's steps once, without saving. |

## Sounds and captures

| Method and path | Purpose |
|---|---|
| GET `/api/sounds`, POST `/api/sounds/upload`, `/api/sounds/play`, `/api/sounds/delete` | The shared audio-file library; upload sends the file as `application/octet-stream` with the name in `X-Sound-Name`. |
| POST `/api/tunes/play` | Play a built-in tune with repeat, volume and pitch. |
| GET `/api/captures`, `/captures/<name>`, POST `/api/captures/delete` | List, serve (with byte ranges) and delete captures. |
| POST `/api/captures/message` | Upload a phone recording as `application/octet-stream`. |

## Pages and assets

`/`, `/sentry`, `/hardware` (and `/tests`), `/dashboard.css`, the page scripts, and
`/local-ca.crt` when a local CA is configured.

See also: [web dashboard](web-dashboard.md), [security model](security.md).

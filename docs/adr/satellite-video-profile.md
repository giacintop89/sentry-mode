# Satellite video profile

**Status:** qualified for one CSI camera on a Zero W, 640x480 at 10 fps
**Gate:** G0 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md), and the PR-08 gate
**Decision:** raw H.264 inside mutual TLS, to a gateway written for the hub. No MediaMTX, and no ffmpeg on the node.

The Zero W encodes and sends; the Pi 5 decodes, detects and records. This page records
which transport was actually proved, because a string that the configuration validator
accepts is not a working stream: protocol, container and backend have to match, and
`camera.device` reaching `cv2.VideoCapture` only means the URL was well formed.

## The pipeline

```text
CSI camera
  -> rpicam-vid, hardware H.264, 640x480 at 10 fps, parameter sets before every keyframe
  -> the agent copies the encoder's output, unchanged, into one TLS 1.3 connection
  -> the hub's media gateway: certificate, node, stream, source and token all checked
  -> bytes before the first SPS dropped, the rest handed to a supervised ffmpeg
  -> raw BGR frames -> latest-frame slot -> shared inference scheduler
```

The browser never sees the node's stream. The hub is its only reader, and anything a
person watches is the hub's own MJPEG.

## Why not RTSPS and MediaMTX

The plan named RTSPS through MediaMTX as the candidate. Measured on the board, the
simpler shape wins on every point that mattered:

- **Nothing to install on the node.** `rpicam-vid` 1.12.0 on the Zero is built without
  libav, so it cannot write RTSP or any container itself, and `ffmpeg` is not installed.
  Adding ffmpeg to an ARMv6 board with 426 MB of RAM to repackage a stream nobody but the
  hub reads buys nothing.
- **One reader.** RTSP earns its complexity when several clients read one publication.
  Here the hub is the only reader by design, so a media server would be a second daemon
  with its own authentication, its own API and its own version to pin, in front of a
  single consumer.
- **The same trust as MQTT.** The node already holds a certificate from the satellites
  CA, and the registry already pins it. The gateway uses exactly that: no passwords, no
  credentials in URLs, and an approval or revocation means the same thing on both links.
- **Revocation closes what is open.** The gateway owns the sockets, so ending a node's
  session, revoking it, or not renewing a stream shuts an open publication at once, not
  only the next one. With an external media server that needs its admin API.
- **No UDP**, so nothing to tunnel.

What this gives up: a stream cannot be opened by a generic RTSP client for debugging.
`ffplay` can still play a capture of the elementary stream, and a stream that works here
proves the hub side independently of any media server.

## The protocol on the media port

1. The hub opens a stream in the gateway: a stream id, a 256-bit one-off token (only its
   hash is kept) and an expiry.
2. The hub sends `video_start` over MQTT with the stream id, the source, the port and the
   token, stamped with the node's current epoch. The node takes it only with a live events
   grant from that epoch. The host is never in the command: the node connects to the host
   in its own configuration, so a command cannot point a camera at anybody else.
3. The node connects with its certificate (TLS 1.3, hub certificate verified against the
   satellites CA and the address used) and sends one JSON line:
   `{"schema_version": 1, "stream_id", "source_id", "token"}`.
4. The gateway checks, in this order: the certificate chains to the CA; its common name is
   an approved node whose pinned fingerprint matches; the stream exists and belongs to that
   node; the source matches; the token matches (constant-time); the stream has not expired;
   nobody else is publishing it. It answers one line, `{"ok": true}` or
   `{"ok": false, "detail": ...}`, and after `ok` the connection carries only H.264.
5. The hub renews with `video_renew` every `renew_every_seconds`, and the gateway expiry
   moves with it. `video_stop`, a revocation, the node's session ending or the expiry
   passing each close the connection from the hub side; the node's own copy of the expiry
   stops the encoder even if the hub has vanished.

A refusal ends the stream on the node; a lost connection, a silent encoder or one that
exits is retried with a growing wait, with a fresh encoder each time so the hub starts at a
keyframe.

## Frozen values

| Setting | Value | Where |
|---|---|---|
| Encoder | `rpicam-vid --nopreview --timeout 0 --codec h264 --inline --intra N --width W --height H --framerate F --bitrate B --flush --output -` | `satellite/.../camera/profile.py`, argv, `shell=False` |
| Sizes | 320x240, 640x480, 1280x720 | node TOML, checked as pairs |
| Rate | 1–15 fps, default 10 | node TOML |
| Bitrate | 100–4000 kbit/s, default 1000 | node TOML |
| Keyframe interval | 0.5–10 s, default 2 | node TOML; `--intra` is this times the rate |
| Stream lifetime | 60 s, renewed every 20 s | hub `satellites.media` |
| Stall | 5 s without bytes ends the connection | hub `satellites.media` |
| Decoder | ffmpeg, `-probesize 32 -analyzeduration 0 -threads 1`, raw BGR at the declared size | `vision/network.py` |
| Decoder backlog | 4 MiB; beyond it the connection is dropped, never trimmed | hub `satellites.media` |

Compressed bytes are never dropped to catch up: that would corrupt every frame that refers
to them. A decoder that falls behind costs the connection, and the node reconnects at the
next keyframe. Only decoded frames are dropped, by keeping the newest.

## Measured

### G0, 16–17 September 2026

- `zero_w_qualify.py --video 10`: 10 s at 640x480/10 fps, **1028 KiB, 0.76 Mbit/s** in
  daylight, one second before the first frame.
- `rpicam-vid -n -t 30000 --codec h264 --inline --intra 20 --width 640 --height 480
  --framerate 10 --bitrate 1000000 --flush -o -` piped into a Python TLS 1.3 client
  (TLS_AES_256_GCM_SHA384) to the hub: 299 862 bytes in 30.3 s (0.08 Mbit/s, dark night
  scene), first byte after 0.69 s. The capture decodes as **293 frames (9.8 fps)**, High
  profile, 640x480, keyframe every 20 frames.
- On the Zero while streaming: `rpicam-vid` about **12 % CPU and 17 MB RSS**, the Python
  sender about **1.4 % CPU and 15 MB RSS**. Wi-Fi signal −26 dBm at the test position.
- On the hub: decoding the 30 s capture took 1.26 s of CPU, about **4 % of one core**.

### PR-08 gate, 17 September 2026

The hub's StreamCam (640x480, Sentry at 5 fps) and the Zero W's IMX219 through the whole
path above — real certificates, real gateway, the agent's publisher on the node, the real
YOLOX Nano model shared by both cameras at `budget_fps: 10` — for 60 s:

| Measurement | Value |
|---|---|
| Remote frames delivered | **10.0 fps** steady, decoded at 640x480 |
| Remote inferences | 233 (5 per second as asked); 230 frames skipped by design |
| Local inferences at the same time | 145; the StreamCam kept capturing throughout |
| Median inference | 56 ms |
| Zero W while streaming | `rpicam-vid` ~14 % CPU, 17 MB RSS; publisher ~7 % CPU, 19 MB RSS; 282 MB still available |
| Time from lease to first decoded frame | 10.7 s in this rig, of which about 5 s was starting Python over SSH; about 3 s from the node connecting to the first frame |
| Encoder killed on the node halfway | the publisher reconnected with a new encoder; frames flowed again **about 5 s** later, no failure reported to Sentry |
| Lease released | the hub closed the stream; the node's retry was refused with `unknown stream` and it stopped; no `rpicam-vid` or publisher left on the node |
| Stopping the cameras | returned at once; nothing waited on the node |

The local camera ran at 2.5 inferences per second against 5 requested. That comes from its
own capture loop (a wait of one frame period on top of the time a read takes), which is
the same before this change, not from sharing the model.

Covered by tests rather than by the board: a node offline at start, a connection that sends
nothing, a truncated or damaged stream, a dead or silent encoder, a gateway restart, a
session revoked mid-stream, a decoder that stops reading (bounded, then dropped), a decoder
that ignores SIGTERM (killed and reaped), and another node presenting a valid token for a
stream that is not its own.

### Not measured

- **Glass-to-detection latency.** No timestamp travels with the frames yet; the figure above
  is time to first frame, not delay per frame.
- **A second remote camera.** Only one Zero W is available. The gateway, the scheduler and
  the tests take several; the board-level gate for the second one is still open.
- **Wi-Fi at the final install position**, and throughput under loss.
- **1280x720 and rates above 10 fps** are accepted by the schema but were not run on the
  board.

## A local-device setting is not a remote one

`camera.fourcc` (default `MJPG`) and `camera.exposure` arrived on the hub in `5ccc285` and
are UVC negotiations against a local device. A network source cannot honour them, so the
source registry must not offer them for a remote camera, and migrating a `legacy-primary`
whose `device` is a URL must not apply them. The receiver does not steer the encoder: size,
rate and bitrate change by reconfiguring the node, which starts a new stream.

## Outcome

Transport: raw H.264 over mutual TLS to the hub's gateway.
Profile: the frozen values above. The CSI video capability is qualified at 640x480/10 fps
on the Zero W; other sizes and rates, USB cameras and a second remote camera still need
their own run on hardware.

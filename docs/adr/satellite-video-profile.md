# Satellite video profile

**Status:** unqualified — the pipeline has not been run end to end
**Gate:** G0 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)
**Decision:** pending between RTSPS and RTSP inside a verified TLS tunnel

The Zero W encodes and sends; the Pi 5 decodes, detects and records. This page records
which transport was actually proved, because a string that the configuration validator
accepts is not a working stream: protocol, container and backend have to match, and
`camera.device` reaching `cv2.VideoCapture` only means the URL was well formed.

## Candidate pipeline

```text
CSI camera
  -> rpicam-vid, hardware H.264, 640x480 at 10 fps
  -> ffmpeg, stream copy only (remux, never re-encode)
  -> RTSP with RTP interleaved over TCP, inside TLS
  -> MediaMTX on the Pi 5, hub side only
  -> supervised decoder subprocess -> latest-frame slot
  -> shared inference scheduler -> the existing OpenCV DNN detector
```

MediaMTX runs on the hub. Nothing in this profile requires building a media server for
ARMv6.

## Fixed before measuring

- **No software re-encode on the Zero.** `libx264` or any other software encoder on the
  node is out of the profile. FFmpeg is there to repackage a stream the GPU already
  encoded.
- **No plain RTSP in production.** Either the build publishes RTSPS with server
  verification, or RTSP interleaved over TCP goes inside a verified TLS tunnel covering
  RTP and RTCP as well. Credentials in a URL are not encryption.
- **No UDP outside the tunnel**, which is why interleaved TCP is the fallback shape.
- **The receiver does not steer the encoder.** Width, height and frame rate belong to the
  satellite profile; setting `CAP_PROP_*` on the hub is not a command to the remote camera.
- **Overlay, recording and detection belong to the Pi 5.** The node never decodes its own
  video to draw on it.
- **The node does not choose its own path.** The hub assigns
  `satellites/{node_id}/{source_id}`; publication and read are authorised separately.

MJPEG over HTTP stays available as a prototype and compatibility profile. It is not
equivalent, and its bandwidth has to be measured rather than assumed: at 30 kB per frame
and 10 fps it is about 2.4 Mbit/s before overhead, which is an arithmetic example and not a
measurement of this board.

## Measurements to record

| Field | Value | Notes |
|---|---|---|
| `rpicam-vid` version and arguments | | exact argv, `shell=False` |
| Encoder path used | | must be the hardware block |
| Resolution / frame rate requested vs delivered | | |
| Bitrate measured | | over a clip long enough to mean something |
| Keyframe interval | | reconnection time against bitrate cost |
| CPU and RSS on the Zero while streaming | | |
| Wi-Fi throughput and loss at the install position | | |
| Transport proved | | RTSPS, or RTSP in which tunnel |
| TLS verification | | server certificate and SAN for the address used |
| MediaMTX version on the hub | | pinned |
| Decoder backend on the hub | | and how a stalled read is cancelled |
| Glass-to-detection latency | | state whether it is end-to-end or ingress-only |
| Behaviour on encoder kill, network loss, gateway restart | | recovery time |

## Outcome

Transport decision: pending.
Frozen profile values: pending.
Until both are recorded, the video capability of every satellite stays `unqualified`, and
the URL prototype described in the plans is a laboratory configuration, not phase 2.

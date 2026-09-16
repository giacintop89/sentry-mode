# Camera and live video

One camera worker serves every viewer. It is started explicitly and stays active until it is
stopped or the server shuts down.

## Preview

**Start** and **Stop** on the main page control live video; GET `/api/video` is the MJPEG
stream. Preview is capped at 10 frames/second and 960 pixels wide. JPEGs are encoded only
while viewers are connected, plus one initial frame, so a hidden preview encodes nothing.

Stopping video hides the preview but does not necessarily release the camera: Sentry keeps
it while armed. With the preview hidden, Sentry requests up to 640 pixels wide at the
selected detection rate (0.5–5 updates/second) and keeps only the latest frame; showing the
preview restores the configured size and rate. The camera's supported modes decide what is
actually used.

Camera tests and changing the camera device require stopping video and disarming Sentry
first. Snapshots reuse the active camera's latest frame, and temporary files are deleted
after each request.

## Pinning the exposure

`camera.exposure`, **Pinned exposure** on [`/hardware`](hardware-and-devices.md), holds the
device at a fixed exposure instead of letting it choose one per scene. The number is the
whole control: any value above zero pins the device at it, and `0` leaves the exposure
automatic. Automatic exposure lengthens in dim light, which keeps the picture bright but
makes brightness drift as the scene changes, and on some devices costs frame rate. Pinned
frames are darker and consistent.

It does not necessarily raise the delivered rate, and on the StreamCam here it does not:
measured at 1080p MJPG, `effective_fps` stays at 15 whether the exposure is automatic,
pinned at 33 ms or pinned at 7.8 ms, while the device streams 25–28 fps to a raw V4L2
reader. The ceiling is elsewhere — see [the delivered rate](#the-delivered-rate). Use the
[latency measurement](#measuring-latency) to check your own device rather than assuming
either way.

The value is in the 100 microsecond unit V4L2 counts in, so `333` is a 33 ms exposure — the
longest 30 fps allows, and a reasonable starting point for a rate. It is applied when the
device is opened, which is also when a running capture picks up a change, since editing any
capture setting makes it reopen the device on its next pass.

Both cases are set explicitly at every open, because the mode belongs to the device rather
than to the handle: a camera left in manual by an earlier setting stays there until
something asks for automatic again.

## Recording the preview

**Record**, beside Stop, saves what the preview shows as an H.264 MP4 with node-microphone
sound, up to 60 seconds. The button stays lit red while recording; pressing it again, or
stopping video, finishes early and keeps the recording. If the microphone is unavailable the
video is still saved, silent. POST `/api/video/record/start` and `/api/video/record/stop` do
the same, and GET `/api/video/status` reports it under `recording`.

Encoding needs `ffmpeg` with libx264. Recordings land in the
[Captures](captures.md) view.

## Status

GET `/api/video/status` distinguishes preview `running` from `capture_running` and
`monitoring`, and carries detection state, current objects and inference timing.

## Measuring latency

`Camera.measure_latency()` opens the device, waits for the first frame, then times twenty
more. POST `/api/camera/test` answers a `latency` block beside the reported size and rate,
and `sentry-mode camera test` prints the same block.

| Field | What it measures |
|---|---|
| `open_ms` | Opening the device — usually the slowest step, and the one that varies most with USB contention. `null` when something had already opened the device, since that run cannot time it. |
| `first_frame_ms` | Open device to first frame handed over. |
| `frame_interval_ms` | Median interval between frames, after that first one has warmed the device up. |
| `slowest_frame_ms` | Worst interval of the same run; a stall shows here rather than in the median. |
| `effective_fps` | What the node actually receives over the timed window, not what the mode was configured for. |
| `frames` | How many intervals were timed. |

```json
{ "width": 1920.0, "height": 1080.0, "fps": 5.0,
  "latency": { "open_ms": 442.7, "first_frame_ms": 1670.7, "frame_interval_ms": 400.1,
               "slowest_frame_ms": 404.7, "effective_fps": 2.5, "frames": 20 } }
```

## Pixel format and delivered rate

`camera.fourcc` is requested before the size, because a UVC device chooses its pixel format
first and OpenCV would otherwise take whatever the device lists first — on the StreamCam,
uncompressed YUYV, which the device only offers at 5 fps at 1080p and cannot sustain over a
USB 2 link. `MJPG` is the default. `info()` reports the format actually negotiated, so the
camera test shows what the node really got. Measured on a StreamCam at 1920x1080, same
device, same link:

| | YUYV (before) | MJPG (after) |
|---|---|---|
| `fps` reported by the driver | 5.0 | 30.0 |
| `effective_fps` | 2.5 | 15.0 |
| `frame_interval_ms` | 400.1 | 66.9 |
| `slowest_frame_ms` | 404.7 | 74.5 |
| `first_frame_ms` | 1670.7 | 924.8 |

Frames are JPEG-decoded on the way in, which is cheap next to the bandwidth it saves. A
device with no compressed mode keeps working: the request simply fails and the device stays
on its own default, and setting `fourcc` to an empty string skips it deliberately.

## The delivered rate

The node receives half of whatever rate it asks for. Measured at 1280x720 MJPG, and the same
at 1080p:

| Requested | `effective_fps` |
|---|---|
| 15 | 7.5 |
| 24 | 12.0 |
| 30 | 15.0 |
| 60 | 30.0 |

A clean 2:1 at every rate and size rules out the device, the link, the exposure and the JPEG
decode, all of which would impose a fixed ceiling rather than a ratio. The cause is
`CAP_PROP_BUFFERSIZE 1` in `Camera.open()`: with a single buffer this V4L2 driver hands over
every other frame. Reading the same device through OpenCV with nothing else changed:

```
BUFFERSIZE=1 (current)    29.7 fps
BUFFERSIZE untouched      59.8 fps
BUFFERSIZE=3              59.8 fps
```

The single buffer is deliberate — it keeps `read()` returning the newest frame rather than
one queued behind others, which is what a live preview and a detector acting on the present
moment need. Relaxing it trades that freshness for rate, so it has been left alone. The
practical consequence is that a configured rate of 60 is how you receive 30.

Two caps sit above this regardless: the preview renders at most 10 frames/second, and
detection runs at `detection.max_fps`.

This is not glass-to-screen latency. OpenCV hands over a frame that has already spent time
in the camera and the driver, and a browser preview adds more on top; read the numbers as
what the node sees, not as end-to-end. A delivered rate far below the configured one usually
means the device fell back to an uncompressed mode the link cannot carry — compare
`effective_fps` against `fps` and check the supported modes with
`./scripts/detect_camera.sh`.

## When the camera drops out

A USB camera that re-enumerates leaves the open handle pointing at nothing, and every read
fails until it is reopened. The capture retries each read up to six times, a second apart,
closing and reopening the device between attempts, and counts recoveries as `reconnects` in
GET `/api/video/status`. A rising count with the preview still running means the bus is
unstable but the node is coping; when every attempt fails the capture stops with the real
error rather than a stale one. Causes and fixes are in [USB devices](usb-devices.md).

## When something is wrong

`./scripts/detect_camera.sh` lists V4L2 paths and supported modes. Prefer a stable
`/dev/v4l/by-id/...` path — the StreamCam is not necessarily `/dev/video0` — check USB power
and bandwidth, and confirm the account is in the `video` group. OpenCV-reported FPS may
differ from measured FPS, and capture output directories must already exist. Video stays
available if the detector fails.

See also: [object detection](object-detection.md), [captures](captures.md),
[hardware and devices](hardware-and-devices.md), [USB devices](usb-devices.md).

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

## When something is wrong

`./scripts/detect_camera.sh` lists V4L2 paths and supported modes. Prefer a stable
`/dev/v4l/by-id/...` path — the StreamCam is not necessarily `/dev/video0` — check USB power
and bandwidth, and confirm the account is in the `video` group. OpenCV-reported FPS may
differ from measured FPS, and capture output directories must already exist. Video stays
available if the detector fails.

See also: [object detection](object-detection.md), [captures](captures.md).

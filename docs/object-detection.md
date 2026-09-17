# Object detection

Labeled boxes and confidence scores for the 80 common COCO categories, computed on the node.

Install the lightweight, checksum-pinned model once:

```bash
python3 scripts/setup_detection.py
```

Detection uses [YOLOX Nano](https://github.com/Megvii-BaseDetection/YOLOX) through OpenCV DNN
on the CPU. There are no cloud calls and no extra Python inference framework, and the runtime
never downloads the model: a missing model is reported when detection is enabled.

## Behavior

**Object detection**, beside the video controls, toggles it; POST `/api/video/detection`
takes `{"enabled": true|false}`. It starts off by default, can be changed during playback,
and is shared by all viewers. It cannot be turned off while Sentry is armed, because Sentry
needs the results.

A separate worker examines the latest frame at up to two updates/second rather than draining
a queue of old frames. Boxes can lag moving objects slightly and expire after 1.5 seconds.

## More than one camera

Every camera shares one loaded model. Each camera keeps only its newest frame, and the
worker serves the camera whose turn is due, within `budget_fps` inferences a second for all
of them together. A camera that asks for more frames gets more turns, never all of them.
A frame that is replaced before its turn is skipped; the rules do not take a skipped frame
as a sign that something left.

The video status reports, under `detection`, the rate this camera asked for
(`requested_fps`) and the rate it actually got (`effective_fps`). While Sentry is armed, a
motion sensor that fires doubles the rate of every watching camera for ten seconds.

With `isolation: process` the model runs in a child process. A frame it does not answer
within `inference_timeout_seconds` gets the process killed; every camera using it reports
the error, and the next start loads a fresh one. The default, `thread`, keeps the model in
the dashboard's process as before. See [shared inference](adr/shared-inference.md).

## Limits worth knowing

This is detection, not tracking or identification. Several people of the same category are
scene occupancy, not identities. Small or obscured objects, and anything outside the model's
categories, can be missed. Video keeps working if the detector fails, and a detector failure
disarms Sentry.

## Configuration

The `detection` YAML section sets startup enablement, model path, confidence threshold
(0.45 by default), maximum inference rate per camera, `budget_fps` (10 by default, shared by
all cameras), `isolation` (`thread` or `process`) and `inference_timeout_seconds` (10). Sentry's own rate and per-rule confidence are
separate, in [Sentry rules](sentry-rules.md).

See also: [camera and live video](camera-and-video.md), [Sentry rules](sentry-rules.md).

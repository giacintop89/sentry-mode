# Shared inference and camera leases

**Status:** implemented; checked with the primary camera (a fake device) and two synthetic cameras, and measured with YOLOX on the Pi 5 — 17 September 2026
**Gate:** PR-07 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)

Until now there was one camera, one detection worker and one YOLOX network, and each
belonged to the other. A second camera must not bring a second network, a second queue or a
second idea of when a rule last saw something. This increment separates the three before a
real remote camera arrives (PR-08).

## One scheduler owns the model

`vision/scheduler.py` loads the model once and is the only code that calls it.

- **Slots, not queues.** Each camera offers frames into a slot that holds one. A frame
  replaced before its turn is counted as skipped. A frame is served at most once: the
  scheduler remembers the last `(epoch, sequence)` it ran for each camera.
- **Turns.** Each camera asks for a rate. The worker serves the camera whose next turn is
  earliest, and `budget_fps` spaces all inferences. A fast camera gets more turns, but a
  slow one's turn still comes up at its own rate. One camera below 10 FPS is not slowed by
  the default budget of 10, so the old single-camera behaviour is unchanged.
- **Boosts end.** A motion event raises every watching camera's rate for ten seconds. The
  factor is capped at 10 FPS and the duration at 30 seconds. A boosted camera still takes
  turns in order.
- **Confidence.** The model prefilters at the lowest confidence any camera asked for. The
  worker sets it between inferences; no other thread reconfigures the network, as
  `set_sentry` used to. Each camera then keeps only what reaches its own threshold, and
  the rules filter again by their own.
- **Results go back to their frame.** A result carries its camera, epoch and sequence. A
  camera that restarted since the frame was taken drops it, so a box from camera A cannot
  appear on B's preview, and a box from before a restart cannot appear on A's.
- **Failure.** An inference error is reported to every camera using the model, as a
  detector failure always was. With `isolation: process` the model lives in a spawned
  child that takes one request at a time. A request not answered within
  `inference_timeout_seconds` gets the child killed; the next demand spawns another. The
  parent never loads the model in that mode. Frames travel over a pipe; they are at most
  640 pixels wide, and shared memory waits for a measurement that says the pipe is too
  slow.

`DetectionWorker` stays, with the same interface, as one camera's client of the scheduler.
A worker created without a scheduler gets its own, which is what a lone camera always had.

## Leases

`sources/manager.py` adds `Lease` and `DemandTable`. A lease names its source, its purpose
(`preview`, `monitoring`, `recording`) and its owner. Releasing it a second time does
nothing, so a count cannot go below zero and one owner cannot release another's use.
Recordings, from Sentry or from the dashboard button, now hold a lease on the primary
camera. `add_recording(±1)` remains for old callers and can only give back what it took.

The primary camera's preview and Sentry switches stay as they were: they are two booleans
with one owner each, and turning them into leases waits for the promotion of the
normalized path (see below). Other cameras, starting with `SyntheticCamera`, run only
while a lease is held. Each has its own lifecycle lock, and the only join done under that
lock is the camera's own.

`SourceManager` is the table of cameras the hub drives. The planner accepts a `vision`
rule on any camera in it. Photos and videos still come only from the primary camera; the
routing of evidence is PR-09.

## Samples per camera

`Sentry.observe` takes the camera's id. Each camera has its own last-sample time:

- A frame no newer than the last one counts for nothing, so a reused frame never adds to
  `consecutive_detections`.
- A gap longer than `max(2 s, 3 / detection_fps)` resets only the rules watching that
  camera.
- A skipped slot produces no call at all, so it cannot look like an absence.

Arming takes a `monitoring` lease on each non-primary camera that a rule watches, and
disarming or a fault gives it back. With the `isolated` fault policy, a camera that stops
pauses only its rules, and its lease is released.

Rules that only listen to sensors create no demand: the model is not loaded and the
scheduler has no thread.

## Not done here

- The legacy path is served by the same scheduler, but the primary camera's preview and
  Sentry switches are still switches. Before the final promotion they become leases too,
  so that there is one engine.
- `SyntheticCamera` exists for tests and development. Nothing in the configuration creates
  one.
- The subprocess mode is not the default yet. It has only been measured on the bench
  (below), not over days on a running hub.

## Measured on the Pi 5

YOLOX Nano, one 640×360 frame offered to three cameras every 20 ms for 10 seconds, each
camera asking for 10 FPS, budget 30:

| Isolation | Model load | Inferences | Per camera | Median | p95 |
|---|---|---|---|---|---|
| `thread` | 0.05 s | 168 | 56 / 56 / 56 | 57.6 ms | 70.8 ms |
| `process` | 0.50 s | 162 | 54 / 54 / 54 | 59.9 ms | 77.1 ms |

The model saturates at about 17 inferences a second on this board, and the three cameras
share that evenly. The child process costs half a second at start and about two
milliseconds per frame.

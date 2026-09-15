# Sentry-Node — Low-Latency Implementation Plan

**Repository:** `giacintop89/sentry-node`  
**Reviewed branch:** `main`  
**Reviewed commit:** `eb559a3898bc83b6ef7d231c9042d90626d89b78`  
**Review date:** 14 September 2026  
**Status:** Proposed implementation plan; no repository changes or hardware benchmarks performed.

## 1. Decision and scope

Prioritize **fresh observations, independent scheduling, and bounded waiting**, not the highest displayed FPS. Keep OpenCV and the current detector initially. Remove avoidable capture-path work before considering a new inference backend, accelerator, programming language, or web framework.

This plan covers four distinct user-visible outcomes:

1. Scene change to a pan/tilt command, for the proposed self-centering feature.
2. Scene appearance to a confirmed Sentry event and independent action dispatch.
3. Confirmed event to audible speech, including preparation and playback.
4. Camera-to-browser preview and phone-to-speaker audio.

The attached **Vision-Node — Functional Plan for Self-Centering Pan/Tilt Webcam** is the functional baseline, referred to as [P1]. This document is a latency addendum, not a replacement. Preserve its calibrated angular geometry, target state machine, composition deadband, bounded reference generation, independent servo PID layer, mechanical limits, and failure handling. Its spring mode remains optional and disabled in the latency baseline.

The reviewed repository implements capture, YOLOX Nano detection, appearance rules, preview, recording, speech, and live audio. The reviewed tree does **not** contain the pan/tilt control modules proposed in [P1]. Therefore, motion work below is new implementation, not an optimization of an existing servo controller. The physical servo interface, feedback capability, deployed configuration, installed package versions, and measured Pi performance remain unverified. Repository defaults are not evidence of the currently running configuration. [R1–R11]

## 2. Findings from the current implementation

| Priority | Verified code behavior | Latency consequence or risk | Relevant location |
|---|---|---|---|
| P0 | The capture loop calls `stopped.wait(1 / self._rate())` between reads. `_rate()` uses the Sentry detection rate in headless mode and caps preview at 10. | Application sampling is deliberately limited. Upstream camera buffering may add further age; that part needs hardware measurement. | `vision/stream.py`: `_capture`, `_rate` [R2] |
| P0 | Detection submission, overlay drawing, and JPEG encoding occur in the capture thread. `DetectionWorker.submit()` itself resizes each submitted image. | The next read waits for this work, even when inference will later replace that pending image. | `vision/stream.py`, `vision/detection.py` [R2, R3] |
| P0 | Headless, preview, and recording demand change the requested camera configuration. A change closes/reopens the device and reads startup frames. | Opening preview or starting recording can interrupt the observation stream. | `VideoStream._camera_config`, `_capture` [R2] |
| P0 | The camera adapter requests width, height, FPS, and one buffer without checking `set()` results. Stream status largely reports configured rather than observed timing. | The runtime cannot establish whether the camera is delivering the requested mode or fresh frames. | `hardware/camera.py`, `VideoStream.status` [R2, R4] |
| P0 | The timestamp named `captured` is generated in `DetectionWorker.submit()`, after resizing. Raw frames have no propagated sequence/timing record. | Reported age excludes acquisition and earlier processing. `inference_ms` is not end-to-end latency. | `vision/detection.py` [R3] |
| P1 | Sentry defaults to 2 detections/s and three consecutive detections. Its configuration currently allows up to 5 detections/s. | With ideal regular sampling, confirmation occurs about 1.0–1.5 s after appearance, before inference/action costs. This is a policy delay, not only a compute problem. | `sentry/config.py`: `SentryConfig`, `Rule` [R5] |
| P1 | The Sentry stale-sample tolerance is `max(2.0, 3 / detection_fps)` seconds. Overlay freshness is 1.5 seconds. | These tolerances must not be reused for moving a camera. | `Sentry.observe`, `DetectionWorker.overlay/status` [R3, R6] |
| P1 | `DetectionWorker` has one result callback; `Sentry` assigns `self.observe` to it. | Adding tracking by assigning another callback would disconnect Sentry. A bounded fan-out is required. | `vision/detection.py`, `sentry/engine.py` [R3, R6] |
| P1 | Speech, SSH, and Telegram execute through the same action worker. Speech can wait for the audio lock and then synthesize/play while owning it. | A busy speaker or slow network action can delay unrelated actions behind it. | `Sentry._worker` [R6] |
| P1 | Piper runs in a new process for each utterance; speech is fully rendered, checked, converted with FFmpeg, then played. Default lead-in is 1000 ms and tail padding 750 ms. | Speech has explicit startup silence plus preparation cost. Tail padding also extends speaker occupancy. | `audio/speech.py`, `config.py` [R7, R8] |
| P2 | Phone audio uses 100 ms PCM packets, serialized HTTP requests, a frontend pending limit of 12, and a backend queue of 12 chunks. | Backlog can reach substantial durations; reducing packet size alone can worsen request-rate limitations. | `talk.js`, `audio/talk.py` [R9, R10] |

### Preserve what is already good

The detector already uses a replaceable pending slot and a separate inference thread. Inference pacing already subtracts inference duration from the requested interval. The MJPEG publisher already shares one encoded frame between viewers. Encoding is skipped when preview is hidden. Recording and photo-series jobs already have background workers. Keep those properties rather than proposing them as wholly new changes. [R2, R3, R6]

Also, `queue.get(timeout=0.1)` wakes when a job arrives; it is not an unconditional extra 100 ms delay. Do not spend a performance change on removing that timeout without evidence. The action problem is resource waiting and serial execution, not that timeout alone.

### Derived sampling calculation

For `N` required positive observations at an ideal regular rate `f`, with a target appearing at a uniformly distributed point in the sampling interval:

```text
sampling-only confirmation delay: [(N - 1) / f, N / f)
mean sampling-only delay:         (N - 0.5) / f

N = 3, f = 2 Hz: 1.0–1.5 s; mean 1.25 s
N = 3, f = 5 Hz: 0.4–0.6 s; mean 0.50 s
```

These are derived idealized values, not measured results. Missed detections, capture pacing, inference, startup, and resource contention add delay. Changing the confirmation requirement changes false-trigger behavior and needs an explicit quality test.

## 3. Proposed runtime architecture

```text
One camera owner — continuously acquire at a negotiated hardware rate
    |
    +--> shared latest immutable FramePacket
             |
             +--> detector worker, independently paced
             |       +--> fresh semantic result --> Sentry rule evaluator
             |       +--> fresh semantic result --> target acquisition/revalidation
             |
             +--> tracker worker --> latest TargetObservation
             |                           |
             |                           +--> geometry/filter/deadband
             |                                 --> fixed-rate reference generator
             |                                 --> actuator controller / existing PID
             |
             +--> preview worker --> latest JPEG --> HTTP viewers
             |
             +--> bounded recording/snapshot consumers

Confirmed Sentry events --> bounded action dispatcher
    +--> audio preparation/cache --> one speaker owner
    +--> independent notification/remote-command lanes
    +--> bounded media jobs

Metrics consume compact records; they do not block any of these paths.
```

This diagram defines scheduling responsibilities, not a requirement for one process per box. Begin with threads and short critical sections. Introduce a process boundary only where benchmarks show CPU contention or where a native call needs independently enforceable restart/cancellation.

Do not put HTTP, WebSocket, MQTT, disk storage, an LLM, or the speech queue between target observations and servo references. Networking may configure the controller and report state; it is not the local motion transport.

### Rates are independent controls

| Function | Proposed starting point | Meaning |
|---|---:|---|
| Physical capture during responsive operation | Supported 30 FPS mode | A request to verify on the actual camera, not an assumed supported mode |
| Lightweight tracker after acquisition | 20–30 updates/s | A benchmark target; not a claim that YOLOX runs at that speed |
| Semantic detector / revalidation | 2–5 updates/s initially | Preserve the existing model and Sentry bounds while measuring |
| Pan/tilt reference generator | 100 Hz initially | A software scheduling target; choose hardware command rate separately |
| Actuator PID | Existing supported hardware rate | Preserve [P1]; do not invent feedback or change servo protocol frequency |
| Preview encoding | 5–10 FPS | May be reduced without changing capture or tracking |
| Telemetry/status refresh | 2–5 Hz | Aggregate fast samples rather than render/log every control tick |

Keep a separately selectable economy profile for stationary monitoring. Its lower sampling cadence is an explicit latency/power tradeoff. Do not activate economy mode solely because the preview is hidden while tracking is enabled.

## 4. Timing and data contracts

### 4.1 Frame and observation lineage

Add `core/frame.py` and `core/timing.py` or equivalent small modules. These are proposed new files.

A `FramePacket` should carry:

```text
stream_epoch                 changes on camera reopen/reconfiguration
frame_sequence               strictly increases within that epoch
read_started_ns              local monotonic clock
read_completed_ns            local monotonic clock, before resize/encoding
source_timestamp_ns          optional; mapped to the same clock only when verified
timestamp_origin             driver_SOE / driver_EOF / host_read_return / unknown
clock_mapping_uncertainty_ns  optional; no invented zero-uncertainty mapping
width, height, pixel_format
calibration_id
image                        immutable-by-contract image buffer
```

A result/observation adds the source epoch and sequence, preprocessing/inference times, publication time, selected target ID, confidence/quality, observation type, and measurement timestamp. Preserve separate times for the latest visual tracking measurement and latest semantic revalidation. Prediction does not create a new measurement or refresh semantic confidence.

A frozen dataclass alone does not make an image array immutable. Either transfer ownership of a fresh array with a strict no-mutation policy or copy into an owned buffer at a documented boundary. Never overwrite a buffer still referenced by another worker. Overlay drawing must use its own writable image.

### 4.2 Do not mislabel application receipt as exposure time

The earliest simple OpenCV instrumentation point is immediately after a successful `read()`. This is useful, but it does not reveal exposure time or all driver/USB buffering.

When the backend exposes V4L2 metadata, verify both its timestamp clock and its start-of-exposure/end-of-frame flags. Do not subtract an unrelated camera, browser, or microcontroller clock from Python monotonic time. GStreamer presentation timestamps also need a verified mapping; they are not automatically sensor exposure timestamps. [E2]

If only host read-return timestamps are available, report **read-to-command latency**, label source age unavailable, and use a physical test to establish scene-to-command behavior. An age check based on host receipt alone cannot prove the absence of hidden camera backlog.

### 4.3 Required metrics

| Metric | Definition / purpose |
|---|---|
| `read_duration_ms` | Read completion minus read start; not synonymous with image age |
| `frame_delivery_age_ms` | Read completion minus verified source time, when available |
| `frame_age_at_consume_ms` | Consumer start minus the documented frame timing origin |
| `preprocess_ms`, `inference_ms`, `postprocess_ms` | Separate stage costs |
| `observation_age_at_control_ms` | Age of evidence used by each control tick |
| `read_to_command_ms` | Command submission minus host read completion |
| `source_to_command_ms` | Command submission minus verified source timestamp |
| `command_to_motion_ms` | Measured physical axis response, not a software write duration |
| `appearance_to_confirm_ms` | External/replayed appearance marker to Sentry confirmation |
| `confirm_to_action_start_ms` | Dispatch and resource waiting, per action lane |
| `tts_prepare_ms`, `audio_lock_wait_ms` | Separate synthesis/cache and speaker contention |
| `event_to_audible_ms` | Physical first audible output, independently measured |
| `control_lateness_ms`, `control_overruns` | Fixed-rate scheduling quality |
| `frame_overwrites`, `stale_rejections`, `missed_updates` | Freshness tradeoffs and failure visibility |
| `tracking_available_fraction` | Prevent a system that drops everything from appearing fast |
| `camera_reopens`, `stream_epoch` | Detect interruptions caused by UI/recording changes |
| `cpu`, `rss`, `temperature`, throttling indicators | Detect load and thermal-related degradation |

Use bounded counters/histograms and periodic aggregation. Report p50/p95/p99, maxima where meaningful, sample count, workload, and unavailable/stale durations. Do not add per-stage p95 values and call the sum the measured end-to-end p95; correlate full traces.

## 5. Proposed acceptance budgets

These are initial engineering goals, subject to the first hardware baseline. They are neither measurements nor unconditional guarantees.

| Path / condition | Initial acceptance goal |
|---|---|
| Already acquired target, responsive profile | Read-to-command p95 <= 50 ms; source-to-command p95 <= 100 ms when source timing is verified |
| Valid tracking output | Target 20–30 Hz and >= 95% availability on the agreed visible-target test sequence; report dropped/stale periods |
| Motion observation age | Reject evidence older than a configured initial 150 ms budget; qualify the timestamp origin |
| Reference task at 100 Hz | p99 wakeup lateness <= 5 ms on the target Pi workload; no catch-up burst after an overrun |
| Independent local action lane with resource available | Confirmation-to-dispatch p95 <= 50 ms; not a network-delivery guarantee |
| Cached, prepared speech with warm output path | Cache lookup/preparation-to-playback submission p95 <= 50 ms; measure audible onset separately |
| Preview/recording contention | Core tracking continues; no camera reopen on ordinary preview toggle in the same responsive profile |
| Overload and faults | Bounded memory/work, explicit stale/unavailable state, and defined safe motion stop |

A 100 ms software/vision budget does not promise that an axis can settle through a large angle in 100 ms. Measure mechanical onset, rise/settling time, overshoot, and angular error separately, retaining [P1]'s speed and acceleration limits.

Do not impose this tracking budget on deliberate multi-observation alert confirmation. Report acquisition latency separately from steady-state following latency.

## 6. Implementation work packages

### WP1 — Establish a trustworthy baseline

**Change:** `hardware/camera.py`, `vision/stream.py`, `vision/detection.py`, `sentry/engine.py`, `audio/speech.py`, `web.py`; add `core/frame.py`, `core/timing.py`, `telemetry/latency.py`, and `scripts/benchmark_latency.py`.

Instrument the current behavior before altering scheduling. Carry epoch/sequence/timestamps across capture, detector, Sentry event, action, and eventual control reference. Export compact JSON/CSV summaries without saving camera images by default. Log requested camera settings separately from returned settings and measured delivery cadence.

The benchmark manifest must include repository commit, deployed configuration after persisted overrides, model checksum, Python/OpenCV build, camera backend/mode, audio backend, preview viewer count, recording state, CPU thread policy, and whether the run is cold or warm.

**Acceptance:** every sampled result is traceable to its source; unsupported source timing appears as unavailable; no blocking file/network operation occurs in capture or control; instrumentation overhead is measured. Preserve existing functionality.

### WP2 — Make capture independent of processing and preview

**Change:** `hardware/camera.py`, `vision/stream.py`; add `vision/frame_buffer.py` and `vision/preview.py`.

The capture owner should do only acquire, validate, timestamp, and publish. Remove consumer-rate sleeps from the responsive capture path. Pace preview and detector consumers independently. A normally blocking live-camera read supplies pacing; retain an interruptible backoff/recovery path for errors or an unexpectedly nonblocking source, rather than creating a busy-spin loop.

Use a shared latest-value holder with a condition and per-consumer last-seen sequence. It must not be a destructive queue where the detector steals frames from the tracker or preview. Slow consumers skip superseded frames; they must never make capture wait for capacity. Define immutable buffer lifetime explicitly.

Move `DetectionWorker.submit()` resizing out of the capture thread. Move annotation/JPEG work into the preview worker. Publish a single latest JPEG for all viewers and keep skipping rendering when no viewer requires it. Snapshot encoding is a bounded separate request, not capture-thread work.

Add explicit camera-demand state for preview, Sentry, tracking, and recording. During active tracking, retain one negotiated resolution/rate across preview and recording changes. Downscale downstream. A request for higher-quality media that would require reopening must either be deferred/rejected with an explanation or enter an explicit safe-stop/reconfiguration state. Do not silently trade away tracking continuity.

On Linux, prefer an explicitly selected supported V4L2 backend, with a documented fallback for other platforms/tests. Check setting calls, inspect actual frames/mode, and measure delivered FPS. OpenCV warns that even successful property setting does not guarantee device acceptance; requesting one buffer is not a freshness proof. [E1]

Enumerate supported camera formats before choosing the responsive profile. Compare actual supported low-resolution and medium-resolution modes; do not assume 640x360 is available merely because the current code requests it. Compare MJPEG/raw modes by measured end-to-end age and CPU cost rather than assuming either is universally better. Preserve calibration when resize/crop/FOV changes; simple matrix scaling is valid only for the corresponding pure resize.

If the OpenCV path still exposes unacceptable hidden buffering, add an optional backend rather than rewriting all consumers. GStreamer appsink has bounded-queue/drop controls; configure a one-buffer, drop-oldest policy and verify installed property support. `leaky-type` is a 1.28-era property; older installations use their supported mechanism. Avoid inserting unbounded queues upstream. [E3]

**Acceptance:** a deliberately slow preview encoder does not block capture; a slow detector cannot grow a frame FIFO; preview toggles do not reopen the camera while tracking; shutdown releases the device; unsupported modes are visible rather than silently treated as negotiated.

### WP3 — Improve detector freshness without changing model semantics

**Change:** `vision/detection.py`, `config.py`; add a small result-distribution component if needed.

Keep the current YOLOX Nano baseline and its single-owner network object. At the next permitted inference start, select the newest unprocessed raw packet and preprocess that packet once. Do not preprocess every incoming frame only to discard most of them. Preserve the current pinned export's 416x416 input, BGR conventions, letterboxing, and expected output layout. Changing 416 to 320 is a model/export/postprocessing change, not a safe camera configuration tweak. [R3]

Publish a structured `DetectionResult` rather than a list plus a misleading `captured` float. Reject results from old stream epochs, old arm sessions, or a configured consumer freshness budget. Check age again immediately before consumption, not only before inference.

Replace the single `on_result` ownership assumption with bounded fan-out. Sentry and target acquisition both receive detector evidence without either overwriting the other's callback. Do not execute slow subscribers, formatted logs, synthesis, storage, or network calls on the inference thread. Preserve that each completed semantic result can affect an appearance rule at most once. Count skipped semantic results explicitly. A missing result is neither another positive observation nor proof of absence; reset/invalidate an affected consecutive-confirmation window rather than silently treating a gap as continuous evidence.

Schedule using monotonic deadlines and a maximum allowed rate. Skip missed scheduling slots instead of catching up. The existing worker already subtracts inference time; preserve that useful behavior while adding explicit scheduling and source-age metrics.

Warm the detector explicitly and report ready/warming/error states. Warm-up results must not count toward arming or move hardware. Camera reopen, model replacement, or stop/restart changes the epoch. Do not clear a stop flag and reuse shared state while an old worker might still publish.

Benchmark OpenCV thread counts with the complete workload. Move any `cv2.setNumThreads` policy to a controlled startup point: OpenCV documents that the function is not thread-safe and affects subsequent parallel regions. Do not treat it as a detector-local live-tuning setting. [E6]

**Acceptance:** delayed results cannot relatch a new arm session; duplicate samples do not count as additional detections; detector overload gives skips/stale state instead of latency growth; preprocessing/output parity tests pass for the pinned model.

### WP4 — Add the self-centering fast path

**New implementation based on [P1]:** `vision/tracker.py`, `vision/observation.py`, `vision/geometry.py`, `control/centering.py`, `control/reference_generator.py`, `control/loop.py`, `control/limits.py`, `motion/driver.py` and relevant tests. Names may be adjusted to the repository's conventions.

#### Separate recognition, following, and alert authorization

Use semantic detection to acquire/revalidate the chosen target. Use a measured lightweight tracker between detector updates. Preserve a target-local state machine and target association; a `person` class label alone is not a unique object identity. Multiple matching targets require an explicit selection rule and track continuity.

Tracker updates must not be fed into Sentry as if they were fresh independent semantic detections. Sentry keeps its configurable confidence, count, consecutive-hit, cooldown, absence, and action rules. Motion gets its own fresh-observation gate. Do not make every pan/tilt adjustment wait for the alert's three-hit confirmation.

Retain the detector's source frame when initializing a tracker. Do not initialize an old bounding box directly on an unrelated newer frame. Bridge a valid detection to the newest frame using a tightly bounded history/replay or other validated association. Cap replay work and age; discard/reacquire rather than building a catch-up backlog.

Choose the tracker through on-device tests of drift, occlusion, target switching, and age. No blanket assumption that CSRT, KCF, or optical flow is always the best option. Detection-only remains a fallback when it meets the requested motion budget.

#### Fixed-rate reference generation

The controller reads the latest valid observation and current axis state, applies [P1]'s geometry/filter/deadband/reference limits, and publishes an angle setpoint. It never waits for inference, TTS, UI rendering, or a network acknowledgement.

Use absolute monotonic deadlines. After a missed tick, record the overrun and resume at a future deadline; do not replay a burst of obsolete control steps. Distinguish measured `dt` from a maximum permitted integration interval. Large scheduling gaps trigger the defined safe-stop path, not a large setpoint leap.

Apply measurement-filter updates once per new observation, not once per repeated 100 Hz read of the same camera measurement. Otherwise changing controller frequency changes effective filter behavior and can create false confidence. The bounded reference generator may still update every control tick.

Validate command rate against the real motor controller. With an external controller, include sequence, expiry, and acknowledgements where supported. Never queue a backlog of position commands. With hobby servos, command repetition rate is not necessarily the internal PID rate. Without actual angle feedback, mark position as an estimate and do not claim a true external position-feedback PID.

Keep dry-run as the initial integration mode. Motion requires explicit enable, calibrated geometry, axis direction checks, bounds, and a verified actuator interface. Arming state is not restored into automatic movement on restart.

#### Stale observations and failure handling

Introduce separate `max_tracking_observation_age_ms`, semantic revalidation expiry, target-lost hold duration, and actuator communication watchdog settings. Do not reuse the multi-second Sentry or overlay tolerance for motion.

Once visual evidence expires, stop integrating the old image error. Transition to a bounded deceleration/hold behavior consistent with [P1] and the mechanics. The longer LOST hold period means holding pose, not continuing to chase stale coordinates. Predictive/coasting output does not renew the measurement watchdog.

Camera reopen, calibration change, target switch, and large frame gaps invalidate tracker/filter history. Ordinary Sentry disarm may leave tracking running only when tracking was explicitly enabled as an independent mode and that state is clearly shown. Node shutdown and a motion emergency stop override all modes.

#### Filtering and prediction

Start with the rate-limited reference generator in [P1], not spring mode. Minimize duplicate smoothing while retaining limits and testing stationary-target hunting.

For the plan's EMA, `alpha = 0.30`, the low-frequency group delay is approximately:

```text
T_delay = ((1 - alpha) / alpha) * sample_interval
30 Hz: 77.8 ms
20 Hz: 116.7 ms
10 Hz: 233.3 ms
```

This is a mathematical filter characteristic, not a constant delay at all frequencies or a measured total system delay. It shows why the same numeric alpha should not be copied unquestioningly into a latency-sensitive path. An alternative is a time-constant-based EMA, `alpha = 1 - exp(-dt / tau)`, tuned against jitter and tracking error. Keep deadband/hysteresis and physical acceleration limits; low latency is not permission for violent motion.

Prediction is a later measured improvement. Use an estimated evidence age plus actuator delay with a bounded horizon, not an unconditional 100–300 ms lead. Compensate for the camera's own motion: delayed observations belong to the camera pose at measurement time, not the current pose. Use time-aligned feedback and calibrated geometry where available; commanded pose is an explicitly less reliable approximation. Do not use prediction to disguise buffering or to continue motion after evidence expires.

**Acceptance:** a stationary target stays stable; a moving target is followed without alert-confirmation delays; frozen video cannot cause continued unbounded setpoint integration; axis limits/watchdogs work under CPU load; all [P1] motion tests remain satisfied.

### WP5 — Remove cross-resource action waiting

**Change:** `sentry/engine.py`, `sentry/config.py`; add `sentry/action_dispatcher.py`.

Separate the action dispatch path from execution. Use small bounded resource lanes: one speaker owner, a limited remote-command lane, a limited notification lane, and bounded media admission. Limit simultaneous recorders; the existing background recorder model is not a substitute for a concurrency budget.

Carry event ID, arm-session epoch, created/enqueued/started times, expiry, priority, resource requirement, and cancellation handle. Maintain distinct trace events for preparation, resource wait, and actual execution. Check expiry/cancellation after waiting and immediately before side effects.

Do not silently parallelize all existing rule actions: users may depend on configured order. Preserve legacy order for existing configurations and introduce explicit independent-action groups or dependencies for rules that opt into concurrency. *(Status: the opt-in part of this paragraph is implemented. A rule's actions are now an ordered list of steps, with wait steps and a per-step flag that starts a step together with the one before it; saved configurations without the flag keep their sequential order. The resource lanes, priorities and trace events described below remain proposed.)* Independent Telegram/SSH actions can then start without waiting for a long announcement. Keep ordering within a required sequence and never overlap speaker playback by accident.

Preserve existing fixed SSH commands, timeouts, child-process cancellation, credential sanitization, and Telegram's uncertain-delivery/no-automatic-retry behavior. Faster dispatch must not become duplicate execution. Do not add a durable replay queue for stale motion or announcements.

**Acceptance:** an opted-in independent notification is not blocked by a busy speaker; sequential rules retain their order; queue limits and expiry are deterministic; disarm invalidates queued work and cancels/reaps active jobs.

### WP6 — Make common announcements ready before the event

**Change:** `audio/speech.py`, `audio/playback.py`, `config.py`, action integration; add `audio/speech_cache.py` and optionally `audio/speech_worker.py`.

Prioritize a prepared-phrase cache for the fixed texts already present in Sentry rules. Generate/cache them on explicit preparation or validated configuration update, outside the capture/control path. Do not block the camera while preparing speech, and do not quietly delay readiness without exposing a preparation state.

Key the cache by normalized text, voice and model fingerprint, synthesis engine/version, rate, volume policy, effects, and output format. Include padding/profile in the key if caching a complete padded playback file. Use private bounded storage, atomic publication, and invalidation on voice/effect/config changes. Never play an old phrase under a new cache key.

Move uncached synthesis out of the speaker lock; acquire that lock only when a valid, unexpired playback is ready. Both preparation and playback remain cancellable and subject to resource limits.

For dynamic text, a bounded resident Piper worker may retain the model between jobs. Piper's documented Python API supports loading a voice object and WAV or chunked synthesis. Pin and verify the installed API before integrating it; retaining a model is an implementation choice to benchmark, not a speedup guarantee. [E4]

The current 1000 ms lead-in and 750 ms tail were designed to protect Bluetooth utterances. Do not globally remove them as an untested quick fix. Introduce independently tested cold/warm output profiles. Test decreasing lead-in and tail, cache-hit playback, repeated short phrases, idle wake, first/last-word completeness, and cancellation. A persistent playback stream is optional only after device behavior is measured; it is not permission to keep a microphone recording.

The configured PipeWire latency is 250 ms. Treat it as an audio buffering setting, not a measured total Bluetooth delay. PipeWire documents the lower-buffer/higher-overhead tradeoff. Benchmark supported values while monitoring underruns and preserving a reliable fallback. Measure acoustic onset rather than inferring it from process start or successful pipe writes. [R7, E5]

A cached short acknowledgement sound can provide an immediate local response while a longer utterance is prepared, provided the rule explicitly asks for both and the speaker scheduling remains clear.

**Acceptance:** a cache hit invokes neither Piper nor FFmpeg to regenerate the same asset; expired prepared audio never plays; speaker ownership excludes synthesis waiting; Bluetooth cold-start words are not truncated; actual audible latency is reported separately from software submission.

### WP7 — Optimize live phone audio only after the core path

**Change:** `talk.js`, `pcm-worklet.js`, `audio/talk.py`, transport integration in `web.py`.

Measure client packet age, serialized request duration, server queue duration in milliseconds, player write delay, and acoustic latency. The current frontend waits for each HTTP request to finish before sending the next; a smaller packet duration raises the request rate and is not independently sufficient. Twelve 100 ms chunks can represent about 1.2 seconds in either queue; that is capacity, not mandatory steady-state latency. [R9, R10]

After baseline, introduce a negotiated persistent ordered audio transport and then test 20–40 ms chunks. A WebSocket implementation needs explicit server support: the current server is Python `ThreadingHTTPServer`, not an existing ASGI/WebSocket application. Isolate the transport change and retain the HTTP fallback rather than rewriting the entire dashboard without a measured benefit. [R11]

Bound live-audio buffering by duration, not just packet count. Unlike camera state, PCM samples cannot be freely overwritten without audible discontinuities. Use a defined policy: brief bounded jitter buffering, or explicit reset/cancellation when latency exceeds budget; do not silently replay seconds of stale speech. WebSocket over TCP still needs application-level age/backlog control.

Preserve HTTPS, origin checking, session-token validation, size/rate limits, sequence handling, exclusive speaker ownership, explicit microphone permission, 60-second duration limit, idle timeout, and stop/cancel behavior. Do not introduce always-on microphone capture.

**Acceptance:** a degraded connection fails or recovers according to the documented policy rather than accumulating old speech; cancellation stops output promptly; a valid audio session never affects the motion schedule.

### WP8 — Tune the whole Pi workload, then consider deeper changes

Run the end-to-end benchmark with preview, recording, speech, and representative other server load. Compare OpenCV thread budgets and control lateness, not just isolated detector throughput. Bound background synthesis/transcoding concurrency. Track temperature, frequency changes, and memory pressure without assuming they are the present bottleneck.

When overloaded, reduce preview rate/quality first, then nonessential media work; preserve observation freshness and safe control behavior. Do not silently weaken detector confidence or safety limits to meet a timing chart. Report quality degradation explicitly.

Use the normal scheduler first. CPU affinity or a separate control process is a measured follow-up. Do not enable real-time scheduling blindly. If required actuator timing cannot be maintained on the tested Pi workload, move the hard timing/PID/watchdog responsibility to a suitable dedicated controller while keeping vision high-level. This is contingent on the actual hardware and test results, not a mandatory hardware purchase.

Only after these changes benchmark alternative inference backends, quantization, model exports, or accelerators using the same target-quality dataset and complete path. Do not promise automatic GPU acceleration or a model speedup from changing a flag.

## 7. Regression and hardware test plan

### Deterministic tests — normal CI

Extend existing `tests/unit/test_camera.py`, `test_stream.py`, `test_detection.py`, `test_sentry.py`, `test_speech.py`, `test_talk.py`, and `test_web.py`. Add focused frame-lineage, scheduler, tracker, control, and dispatcher tests as those modules appear. Existing stream tests already cover hidden-preview encoding and camera lifecycle; preserve their intent while updating any assumptions about requested capture size. [R12]

Required cases:

- Latest-value handoff: producer can advance while consumers stall; memory/work stays bounded and each consumer sees a valid independent sequence.
- A slow encoder cannot hold the capture lock or perform work in the capture thread.
- One hardware owner across preview, Sentry, tracking, snapshots, and recording.
- Detector output arriving after disarm/rearm or camera reopen is ignored.
- A duplicate detection never increments a rule twice; a tracker update is not a semantic re-detection.
- Late detector boxes are not blindly applied to a newer frame; reacquisition/replay has a hard work bound.
- One measurement is filtered once even when the control task runs faster than the camera.
- Stale evidence stops further error integration; deadband/limits and the safe-stop trajectory remain valid.
- Deadline overruns skip obsolete work instead of issuing a catch-up command burst.
- Independent action groups bypass an unrelated busy resource; explicit sequential groups preserve order.
- Cached speech invalidates correctly; disarm during preparation cannot later cause playback.
- Queue overload, camera read failure, worker failure, and shutdown leave no active device/child-process leaks.
- New configuration is validated; existing saved rules preserve behavior until explicitly migrated.

Use fake clocks and explicit thread synchronization for logical invariants. Do not make shared CI pass/fail depend on a desktop thread reliably waking within a few milliseconds. Timing budgets belong in the hardware performance suite.

### On-device benchmark matrix

Use the same target sequence, illumination, mode, and configuration for before/after comparisons:

| Case | Purpose |
|---|---|
| Headless Sentry at existing defaults | Establish the real low-power baseline |
| Headless responsive capture, detector enabled | Separate camera freshness from semantic inference cost |
| Acquired target, control dry-run | Measure fast-path timing before energizing actuators |
| Preview hidden / one viewer / several viewers | Verify preview isolation and shared JPEG behavior |
| Recording and snapshot requests | Detect capture reopens and resource contention |
| Cold speech / prepared cache / repeated speech | Separate preparation, transport wake, and audible onset |
| Busy speaker plus notification | Verify action-lane independence and expiry |
| Phone audio under added delay/jitter | Verify age policy, cancellation, and bounded buffering |
| CPU/media stress and sustained thermal run | Measure p99 lateness and safe degradation |
| Camera unplug, frozen frames, detector stall, disarm/rearm | Verify safety and stale-epoch handling |

Inject controlled delays into detector/encoder/test action adapters. Report both successful-operation latency and failure/unavailable time so dropping all work cannot look like a win.

For physical latency, use a visible scene transition and externally observed command/motion, or a shared instrumented trigger plus axis feedback. A high-frame-rate external recording can show both stimulus and response with its quantization uncertainty stated. For audio, record the stimulus marker and actual speaker output in a shared timing setup. Do not subtract a browser wall clock from a Pi timestamp without clock alignment.

Measure cold readiness separately: camera open, warm-up, first inference, target acquisition, and first playable speech. A warm steady-state result does not describe first interaction after boot or idle.

## 8. Configuration and compatibility

Add independent settings for capture profile/rate, preview rate/quality, tracker rate, control rate, age budgets, OpenCV startup thread policy, action resource limits, and speech cache/output profiles. Existing `sentry.detection_fps` must continue to mean semantic detection cadence; do not secretly repurpose it as the motor/control update rate.

The following is a **proposed schema sketch**, not configuration accepted by the reviewed commit:

```yaml
latency:
  profile: responsive
  metrics_enabled: true
  max_tracking_observation_age_ms: 150

capture:
  keep_mode_stable_while_tracking: true
  consumer_handoff: latest

preview:
  max_fps: 8

tracking:
  enabled: false                 # Explicit opt-in after hardware validation
  max_fps: 30
  prediction_enabled: false

control:
  dry_run: true
  reference_hz: 100
  spring_enabled: false

speech_cache:
  enabled: true
  bounded_storage: true
```

Choose actual camera dimensions/format from enumerated supported modes and calibration, not this sketch. Validate all numeric limits and expose the effective profile in status. Display configured, negotiated, and measured values separately.

Sentry loads a private persisted rule configuration, so the effective detection rate may override the YAML/default settings. Edit rules through the supported disarmed workflow or an explicit migration; do not assume changing an example YAML changes the live Sentry state. [R6]

As an interim experiment, test the already supported 5 Hz Sentry setting with the existing three-detection rule and compare quality/latency. This does not implement the fast tracker/control architecture and setting camera FPS to 30 alone does not remove `_rate()`'s current pacing.

## 9. Suggested implementation sequence

| Change set | Deliverable | Merge gate |
|---|---|---|
| 1 — Timing and lineage | Metrics, frame/result metadata, baseline runner | Reliable correlations; source timing correctly qualified |
| 2 — Capture isolation | Continuous responsive capture, latest-value distribution, independent preview | No encoder/inference backpressure; stable active camera mode |
| 3 — Detector scheduling | Preprocess on consume, epoch checks, bounded result fan-out, warm-up | Model parity; no stale/duplicate semantic events |
| 4 — Self-centering integration | Tracker, geometry adapter, fixed-rate bounded references, dry-run and driver contract | [P1] behavior plus stale-frame/overrun tests before motor enable |
| 5 — Actions and speech | Explicit independent lanes, cached phrases, separated preparation/playback | No ordering/security regression; no clipped Bluetooth speech |
| 6 — Live-audio transport | Optional persistent transport and duration-bounded buffering | Improved measured audio age; intact session/cancel behavior |
| 7 — Load tuning and acceptance | Hardware results, quality comparison, deployment profile | Budgets met or shortfall explicitly documented; safe degradation |

Work on actions/speech can proceed independently after timing instrumentation. Do not make capture/motion improvements depend on completing the live-audio transport change.

Each change should remain independently reviewable. Preserve a legacy/economy configuration for rollback. New motion is disabled by default. Do not deploy untested scheduling changes directly over a live actuator installation.

## 10. Definition of done

The implementation is accepted when its effective deployed configuration and benchmark evidence establish that:

- capture, preview, semantic inference, tracking, control, and actions have explicit independent schedules;
- no slow consumer creates an unbounded camera or command backlog;
- frame/result lineage, age, and timestamp limitations are visible;
- an acquired target can be followed without waiting for Sentry alert confirmation;
- old frames, late results, target loss, and worker failures cannot drive continued stale motion;
- physical safety limits and existing event/security/cancellation semantics remain intact;
- ordinary preview/recording use does not reopen the camera during active tracking;
- unrelated action resources do not block each other when explicitly configured as independent;
- common announcements use a valid prepared cache and retain complete audible output;
- before/after p50/p95/p99 results are accompanied by tracking availability, detection quality, workload, and cold/warm conditions;
- [P1]'s geometry, motion, occlusion, target-loss, and mechanical-limit tests still pass.

The first improvement should be **fresher evidence with less waiting**. Faster inference and predictive control come after that foundation is demonstrated.

## 11. Source registry

### Repository sources

All repository references below are pinned to the reviewed commit. Functions are identified above to keep the plan navigable even if later line numbers change.

```text
Base:
https://github.com/giacintop89/sentry-node/blob/eb559a3898bc83b6ef7d231c9042d90626d89b78/

[R1]  docs/architecture.md and the recursive repository tree at the reviewed commit
[R2]  src/sentry_node/vision/stream.py
[R3]  src/sentry_node/vision/detection.py
[R4]  src/sentry_node/hardware/camera.py
[R5]  src/sentry_node/sentry/config.py
[R6]  src/sentry_node/sentry/engine.py (reviewed lines 1–620)
[R7]  src/sentry_node/config.py
[R8]  src/sentry_node/audio/speech.py
[R9]  src/sentry_node/audio/talk.py (reviewed lines 1–215)
[R10] src/sentry_node/talk.js
[R11] src/sentry_node/web.py (reviewed lines 1–115)
[R12] tests/unit/test_stream.py
```

### Attached functional source

[P1] `vision-node-self-centering-functional-plan.md`, supplied in this conversation. Particularly sections 3.1, 5–6, 12–18, 20–24, 30–34, and 36. SHA-256:

```text
3e6cc670007e28e4114930b3da62ee2977b6315be1fed0c6083900fbe5b6087a
```

### Primary technical references checked for this plan

```text
[E1] OpenCV VideoCapture: backend selection and property acceptance caveats
https://docs.opencv.org/4.13.0/d8/dfe/classcv_1_1VideoCapture.html

[E2] Linux V4L2 buffer metadata, timestamp clocks and source flags
https://docs.kernel.org/userspace-api/media/v4l/buffer.html

[E3] GStreamer appsink: bounded queues and versioned drop/leaky controls
https://gstreamer.freedesktop.org/documentation/app/appsink.html

[E4] Piper Python API: persistent voice object, WAV and chunked synthesis interfaces
https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md

[E5] PipeWire pw-cat / pw-play latency option and buffering tradeoff
https://docs.pipewire.org/page_man_pw-cat_1.html

[E6] OpenCV setNumThreads: scope and thread-safety warning
https://docs.opencv.org/4.13.0/db/de0/group__core__utils.html
```

External references document API behavior, not benchmark results for this repository. Hardware timing numbers in this document are proposed goals or explicitly labeled calculations.

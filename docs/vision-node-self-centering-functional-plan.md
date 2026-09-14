# Vision-Node — Functional Plan for Self-Centering Pan/Tilt Webcam

## 1. Goal

Implement a self-centering camera function for a webcam mounted on a motorized pan/tilt mechanism.

The system shall:

- detect or recognize a predefined target in the camera field of view;
- estimate the target direction relative to the optical axis;
- generate smooth pan and tilt references;
- command the existing pan and tilt PID loops;
- keep the target inside a configurable composition zone;
- avoid unnecessary motion caused by detector noise;
- recover gracefully when the target is temporarily or permanently lost.

The first implementation should prioritize robustness and simple tuning.

A critically damped spring may be added later as an optional reference-shaping mode, but it is **not required for the baseline implementation**.

---

# 2. High-Level Architecture

```text
Camera frame
    ↓
Target detector / recognizer
    ↓
Target tracker
    ↓
Target observation
    ↓
Camera geometry
(pixel → angular error)
    ↓
Signal filtering
    ↓
Composition deadband
    ↓
Reference generator
    ↓
Pan / Tilt setpoints
    ↓
Servo PID controllers
    ↓
Pan / Tilt servos
```

The vision layer and the actuator layer shall remain separate.

The vision system decides:

> Where should the camera point?

The PID loops decide:

> How should the servos reach that position?

---

# 3. Functional Modules

## 3.1 Camera Input

Responsibilities:

- acquire frames from the webcam;
- provide monotonic timestamps;
- expose frame width and height;
- expose camera calibration data;
- detect capture failures;
- keep image acquisition independent from servo control frequency.

Suggested interface:

```python
class CameraFrame:
    image
    timestamp
    width
    height
```

The vision update frequency may be lower than the servo control frequency.

Example:

```text
Camera / vision:      20–30 Hz
Servo control loop:   50–200 Hz
```

Do not make PID execution depend directly on detector inference time.

---

# 4. Target Detection

The detector shall identify the predefined object.

The centering controller must not depend on one specific detection technology.

Possible implementations include:

- object detection model;
- template matching;
- ArUco marker;
- feature matching;
- face recognition;
- color / shape recognition;
- custom OpenCV detector.

Standard output:

```python
class Detection:
    detected: bool
    confidence: float

    x1: float
    y1: float
    x2: float
    y2: float

    cx: float
    cy: float
```

Where:

```text
cx = (x1 + x2) / 2
cy = (y1 + y2) / 2
```

---

# 5. Target Tracking

Recognition should acquire the target.

Tracking should maintain the target between recognition updates when useful.

Recommended structure:

```text
Recognizer
    ↓
Target acquired
    ↓
Tracker
    ↓
Continuous target estimate
```

Possible tracker implementations:

- OpenCV CSRT;
- OpenCV KCF;
- detector-only tracking;
- Kalman-assisted tracking;
- detector + optical flow.

The tracker shall expose a common target observation independent of implementation.

---

# 6. Target Observation Model

Use a common data structure:

```python
class TargetObservation:
    valid: bool
    timestamp: float
    confidence: float

    cx: float
    cy: float

    width: float
    height: float

    vx_px: float = 0.0
    vy_px: float = 0.0
```

Optional later extensions:

```text
target ID
target class
estimated distance
appearance descriptor
tracking quality
predicted position
```

---

# 7. Camera Calibration

The camera should be calibrated using OpenCV.

Store:

```text
fx
fy
cx
cy
distortion coefficients
```

Camera matrix:

```text
K =

[ fx   0   cx ]
[  0  fy   cy ]
[  0   0    1 ]
```

Recommended OpenCV tools:

```python
cv2.calibrateCamera(...)
cv2.undistortPoints(...)
```

Calibration should be stored persistently.

Example:

```text
config/
    camera_calibration.yaml
```

---

# 8. Pixel-to-Angle Geometry

The controller should operate primarily on angular error rather than raw pixel error.

For target coordinates:

```text
u = target center X
v = target center Y
```

Pan angular error:

```text
pan_error =
atan2(u - cx, fx)
```

Tilt angular error:

```text
tilt_error =
atan2(v - cy, fy)
```

Convert to degrees if desired:

```python
angle_deg = math.degrees(angle_rad)
```

Output:

```python
class AngularObservation:
    pan_error_deg: float
    tilt_error_deg: float
```

This makes controller behavior less dependent on:

- image resolution;
- crop size;
- camera FOV;
- camera model.

---

# 9. Coordinate Conventions

Choose one convention and keep it everywhere.

Recommended:

```text
positive pan error  → target is right of optical axis
negative pan error  → target is left

positive tilt error → target is above optical axis
negative tilt error → target is below
```

Servo direction inversion should be handled in hardware configuration, not inside vision geometry.

Example:

```yaml
pan:
  inverted: false

tilt:
  inverted: true
```

---

# 10. Composition Target

Do not hard-code image center everywhere.

Define a configurable composition point:

```text
target_x = 0.50
target_y = 0.50
```

Normalized coordinates:

```text
0.0 → left / top
1.0 → right / bottom
```

Default:

```text
0.5, 0.5
```

Later this enables:

- headroom;
- rule-of-thirds framing;
- lead room;
- operator-defined composition.

---

# 11. Deadband / Comfort Zone

The camera should not continuously react to tiny target movements.

Use an elliptical deadband around the desired framing point.

Normalized error:

```text
ex
ey
```

Deadband condition:

```text
(ex / dx)^2 + (ey / dy)^2 <= 1
```

Where:

```text
dx = horizontal deadband radius
dy = vertical deadband radius
```

Initial suggested values:

```text
horizontal: 3–6 % of frame width
vertical:   4–8 % of frame height
```

Tilt may intentionally have a larger dead zone than pan.

When the target is inside the deadband:

```text
camera reference remains unchanged
```

---

# 12. Signal Filtering

Detector noise shall be filtered before reference generation.

Baseline implementation:

```text
Exponential Moving Average
```

Example:

```python
filtered = alpha * measurement + (1 - alpha) * previous
```

Possible later upgrade:

```text
One Euro filter
```

Possible advanced upgrade:

```text
Kalman filter
```

Do not filter the final PID output as the primary anti-noise mechanism.

Preferred order:

```text
measurement
    ↓
filter
    ↓
deadband
    ↓
reference generator
    ↓
PID
```

---

# 13. Baseline Reference Generator

The initial implementation should use a simple rate-limited reference generator.

Input:

```text
pan angular error
tilt angular error
current pan reference
current tilt reference
dt
```

Output:

```text
new pan reference
new tilt reference
```

Concept:

```text
target angular error
    ↓
requested reference change
    ↓
velocity limit
    ↓
acceleration limit
    ↓
new servo setpoint
```

Example:

```python
desired_velocity = gain * angular_error

desired_velocity = clamp(
    desired_velocity,
    -max_velocity,
    +max_velocity
)
```

Then:

```python
velocity = slew_limit(
    current_velocity,
    desired_velocity,
    max_acceleration,
    dt
)
```

Finally:

```python
reference += velocity * dt
```

This provides smooth camera movement without introducing another full control loop.

---

# 14. Pan and Tilt Shall Be Tuned Independently

Use separate parameters.

Example:

```yaml
pan:
  max_velocity_deg_s: 70
  max_acceleration_deg_s2: 180
  tracking_gain: 3.0

tilt:
  max_velocity_deg_s: 45
  max_acceleration_deg_s2: 100
  tracking_gain: 2.0
```

Recommended behavior:

```text
Pan:
- faster
- more predictive
- more responsive

Tilt:
- slower
- more damped
- larger dead zone
```

---

# 15. Servo PID Layer

The pan and tilt PIDs already control mechanical motion.

Each loop receives:

```text
setpoint angle
measured servo / axis angle
```

PID error:

```text
error = setpoint - measured_angle
```

The centering function shall never directly overwrite PID output.

Architecture:

```text
Vision centering
    ↓
angle reference
    ↓
servo PID
    ↓
actuator command
```

---

# 16. PID Feedback Requirement

The preferred implementation should use actual axis position feedback.

Possible sources:

- encoder;
- servo position feedback;
- motor controller feedback;
- calibrated commanded position for smart servos.

If no real position feedback is available, the system may initially use commanded angle as an estimate, but this should be documented as open-loop position estimation.

---

# 17. Optional Critically Damped Spring Mode

A critically damped spring can later replace the baseline reference generator.

It should remain a **motion shaping option**, not part of the actuator PID.

Model:

```text
x¨ + 2ζω x˙ + ω²x = ω²xtarget
```

For critically damped behavior:

```text
ζ = 1
```

Equivalent acceleration form:

```text
acceleration =
ω² * position_error
- 2 * ω * velocity
```

Implementation concept:

```python
a = omega**2 * error - 2 * omega * velocity

velocity += a * dt
reference += velocity * dt
```

The result shall still pass through:

```text
velocity limits
acceleration limits
mechanical position limits
```

---

# 18. When to Use Spring Mode

Spring mode is useful when:

- visual smoothness matters;
- camera motion should appear more organic;
- setpoint changes are visually abrupt;
- the detector output is stable but camera motion still looks mechanical.

Spring mode is not required when:

- rate-limited tracking already looks good;
- target motion is slow;
- mechanical servo control already provides sufficient smoothing.

---

# 19. Nonlinear Response — Optional

Later, reference speed may increase nonlinearly with angular error.

Example:

```text
response = k1 * e + k3 * e^3
```

Effect:

```text
small error  → very soft correction
large error  → strong correction
```

This can make the center region calm while allowing fast recovery near image edges.

Do not add this until the linear baseline is tested.

---

# 20. Target State Machine

Do not drive motion directly from a single detection boolean.

Recommended states:

```text
IDLE
  ↓
CANDIDATE
  ↓
ACQUIRED
  ↓
TRACKING
  ↓
LOST
  ↓
SEARCHING
```

---

## 20.1 IDLE

No active target.

Camera behavior:

```text
hold current pose
```

Optional later:

```text
home position
idle scan
```

---

## 20.2 CANDIDATE

A possible target has been detected.

Require stable detection for:

```text
N frames
```

Example:

```text
3 consecutive frames
```

Then transition to:

```text
ACQUIRED
```

---

## 20.3 ACQUIRED

Initialize:

```text
filters
tracker
target history
velocity estimation
```

Then transition to:

```text
TRACKING
```

---

## 20.4 TRACKING

Normal centering active.

Pipeline:

```text
detect / track
    ↓
geometry
    ↓
filter
    ↓
deadband
    ↓
reference generator
    ↓
PID
```

---

## 20.5 LOST

Temporary target loss.

Behavior:

```text
hold current pan/tilt reference
```

Do not immediately return to center.

Recommended hold period:

```text
0.3–1.0 seconds
```

If target returns:

```text
TRACKING
```

Otherwise:

```text
SEARCHING
```

---

## 20.6 SEARCHING

Optional first release behavior:

```text
hold current pose
```

Future behavior:

```text
scan pattern
return home
look toward last known velocity
```

---

# 21. Target Velocity Estimation

Estimate target image motion:

```text
vx
vy
```

Baseline:

```python
vx = (cx_now - cx_previous) / dt
vy = (cy_now - cy_previous) / dt
```

Filter this estimate before use.

This is not required for baseline centering.

It enables later:

- prediction;
- lead room;
- target motion classification;
- feed-forward control.

---

# 22. Predictive Tracking — Later Feature

Future target prediction:

```text
future_position =
current_position + velocity * prediction_time
```

Example prediction horizon:

```text
100–300 ms
```

Then center using predicted target position rather than current position.

Do not enable prediction before baseline tracking is stable.

---

# 23. Mechanical Limits

Define hard limits:

```yaml
pan:
  min_deg: -90
  max_deg: 90

tilt:
  min_deg: -35
  max_deg: 45
```

References must always be clamped before reaching the PID.

The PID output should also be saturated independently.

---

# 24. Software Safety Limits

Required protections:

- maximum pan speed;
- maximum tilt speed;
- maximum acceleration;
- maximum setpoint step;
- servo command bounds;
- watchdog timeout;
- camera failure handling;
- detector failure handling.

If the control process stops receiving valid updates:

```text
hold current pose
```

or:

```text
disable actuator output
```

depending on hardware safety requirements.

---

# 25. Vision Confidence Rules

Tracking should require a configurable confidence threshold.

Example:

```yaml
target:
  acquire_confidence: 0.70
  maintain_confidence: 0.50
```

Using a lower maintain threshold avoids target dropouts once tracking is established.

---

# 26. Telemetry

Expose at minimum:

```text
target detected
target confidence

target pixel coordinates
target angular error

filtered angular error

pan reference
tilt reference

pan measured angle
tilt measured angle

pan PID output
tilt PID output

tracking state

frame rate
vision latency
control loop rate
```

Telemetry should be loggable.

---

# 27. Debug Overlay

The OpenCV debug frame should optionally display:

```text
bounding box
target center
desired composition point
deadband ellipse
filtered target point
pan angular error
tilt angular error
tracking state
confidence
```

Example:

```text
+-------------------------------------+
|                                     |
|            deadband                 |
|          ___________                |
|         /     +     \               |
|        |      ●      |              |
|         \___________/               |
|                                     |
| target: TRACKING                    |
| pan error:  +4.2°                   |
| tilt error: -1.1°                   |
+-------------------------------------+
```

Where:

```text
+ = desired composition point
● = target
```

---

# 28. Configuration

Suggested configuration file:

```yaml
camera:
  calibration_file: config/camera_calibration.yaml

target:
  class_name: predefined_target
  acquire_confidence: 0.70
  maintain_confidence: 0.50

composition:
  x: 0.50
  y: 0.50
  deadband_x: 0.05
  deadband_y: 0.07

filter:
  type: ema
  alpha: 0.30

tracking:
  mode: rate_limited

pan:
  min_deg: -90
  max_deg: 90
  max_velocity_deg_s: 70
  max_acceleration_deg_s2: 180
  tracking_gain: 3.0

tilt:
  min_deg: -35
  max_deg: 45
  max_velocity_deg_s: 45
  max_acceleration_deg_s2: 100
  tracking_gain: 2.0

spring:
  enabled: false
  omega_pan: 5.0
  omega_tilt: 3.5
  damping_ratio: 1.0
```

---

# 29. Proposed Software Structure

```text
vision-node/
│
├── camera/
│   ├── capture.py
│   ├── calibration.py
│   └── geometry.py
│
├── vision/
│   ├── detector.py
│   ├── tracker.py
│   ├── observation.py
│   └── filters.py
│
├── control/
│   ├── centering.py
│   ├── reference_generator.py
│   ├── spring_reference.py
│   ├── pid.py
│   └── limits.py
│
├── motion/
│   ├── pan_axis.py
│   ├── tilt_axis.py
│   └── servo_driver.py
│
├── state/
│   └── target_state_machine.py
│
├── telemetry/
│   ├── logger.py
│   └── overlay.py
│
└── config/
    ├── tracking.yaml
    └── camera_calibration.yaml
```

---

# 30. Core Centering API

Suggested API:

```python
class CenteringController:

    def update(
        self,
        observation,
        current_pan_deg,
        current_tilt_deg,
        dt,
    ):
        ...

        return PanTiltReference(
            pan_deg=...,
            tilt_deg=...,
        )
```

The controller shall not directly command servos.

---

# 31. Processing Sequence

Per vision update:

```text
1. Acquire frame

2. Detect / track target

3. Validate target confidence

4. Update target state machine

5. Compute target center

6. Undistort target coordinates

7. Convert pixels to pan/tilt angular error

8. Filter angular error

9. Apply composition deadband

10. Generate pan/tilt reference

11. Clamp reference to mechanical limits

12. Publish reference to servo controller

13. Update telemetry

14. Draw debug overlay
```

Servo loop independently executes:

```text
1. Read axis position

2. Read latest pan/tilt reference

3. Update PID

4. Saturate output

5. Command servo

6. Repeat at fixed control frequency
```

---

# 32. Implementation Phases

## Phase 1 — Geometry

Implement:

- camera calibration loading;
- pixel-to-angle conversion;
- coordinate conventions;
- test with manually selected points.

Acceptance:

```text
Known image points produce correct angular directions.
```

---

## Phase 2 — Detection

Implement:

- predefined target detection;
- bounding box;
- confidence;
- target centroid;
- OpenCV debug overlay.

Acceptance:

```text
Target is reliably detected while stationary.
```

---

## Phase 3 — Target State Machine

Implement:

```text
IDLE
CANDIDATE
TRACKING
LOST
```

Acceptance:

```text
Single bad frames do not cause abrupt tracking state changes.
```

---

## Phase 4 — Filtering and Deadband

Implement:

- EMA filtering;
- elliptical deadband;
- separate pan and tilt deadband.

Acceptance:

```text
Stationary target does not cause servo hunting.
```

---

## Phase 5 — Rate-Limited Reference Generator

Implement:

- angular error gain;
- velocity limit;
- acceleration limit;
- mechanical angle clamps.

Acceptance:

```text
Large target errors result in smooth movement without abrupt servo commands.
```

---

## Phase 6 — PID Integration

Connect:

```text
centering reference
    ↓
existing pan PID

centering reference
    ↓
existing tilt PID
```

Acceptance:

```text
Camera centers on a stationary target from multiple starting positions.
```

---

## Phase 7 — Dynamic Target Testing

Test:

- slow target;
- fast lateral target;
- target entering frame;
- target leaving frame;
- temporary occlusion;
- detection jitter.

Tune:

```text
deadband
filter strength
tracking gain
velocity limits
acceleration limits
PID gains
```

---

## Phase 8 — Optional Spring Mode

Add only after baseline tracking is satisfactory.

Implement:

```text
critically damped reference generator
```

Compare against baseline:

```text
rate-limited mode
vs
spring mode
```

Evaluate:

```text
tracking lag
overshoot
visual smoothness
reacquisition behavior
mechanical oscillation
```

Keep both modes configurable until testing determines whether spring mode provides a meaningful advantage.

---

# 33. Test Cases

## Static Target

Place target:

```text
center
left
right
top
bottom
corners
```

Verify:

```text
correct movement direction
stable final framing
no oscillation
```

---

## Detector Noise

Keep target stationary.

Verify:

```text
camera remains effectively still inside deadband
```

---

## Slow Target Motion

Move target slowly across frame.

Verify:

```text
continuous smooth following
```

---

## Fast Target Motion

Move target quickly.

Verify:

```text
velocity limiting works
camera does not produce violent corrections
```

---

## Occlusion

Hide target briefly.

Verify:

```text
camera holds pose
tracking resumes without reset
```

---

## Target Loss

Remove target.

Verify:

```text
state transitions to LOST
then SEARCHING or IDLE according to configuration
```

---

## Mechanical Limit

Move target beyond reachable field.

Verify:

```text
setpoints remain inside mechanical limits
PID does not wind up indefinitely
```

---

# 34. Metrics

Track at least:

```text
mean angular centering error
95th percentile angular error

settling time
overshoot
target loss count

servo command rate
maximum servo velocity
maximum servo acceleration

vision FPS
vision latency
control loop frequency
```

Optional comparison metric:

```text
rate-limited reference
vs
critically damped spring
```

---

# 35. Definition of Done — First Release

The feature is complete when:

- the predefined target can be reliably recognized;
- camera calibration is loaded correctly;
- target coordinates are converted into angular error;
- target noise is filtered;
- a configurable deadband prevents micro-corrections;
- pan and tilt references are generated smoothly;
- references respect velocity, acceleration and angle limits;
- existing PID loops receive angle references;
- target loss does not cause uncontrolled movement;
- a debug overlay shows the complete tracking state;
- parameters are configurable without code changes;
- static and moving-target test cases pass.

The critically damped spring is explicitly **not** required for first-release completion.

---

# 36. Preferred Baseline

Start with:

```text
Detection
    ↓
Camera geometry
    ↓
EMA filter
    ↓
Elliptical deadband
    ↓
Rate-limited reference generator
    ↓
Pan / Tilt PID
```

Only after this behaves well should the following be tested:

```text
Critically damped spring reference generator
```

This keeps the first implementation simple, measurable and easy to tune while leaving room for more natural camera motion later.

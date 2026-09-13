# Architecture

CLI / explicit HTTP control dashboard → validated settings → hardware adapters.
The idle application uses the same hardware status inspection as the CLI. Core dataclasses
carry device metadata and availability without exposing subprocess output.

```text
Logitech StreamCam → camera (OpenCV headless) → frame capture / future perception
                  → microphone (Pulse / ALSA) → future speech / agent layer
                                               ↓
                                     speaker (Pulse / ALSA) → Bluetooth speaker
```

OpenCV handles camera acquisition, image encoding, and local object detection through the
camera and vision modules. Audio subprocesses have bounded timeouts and are reaped on failure. Native PipeWire
recording receives SIGINT after two seconds to finalize its WAV header, with kill/reap
cleanup if it fails to stop. Discovery falls back from Pulse to native PipeWire to ALSA. Temporary test recordings and tones are deleted.
Status acquisition releases camera handles before the service waits for termination.
Configuration precedence is environment → explicit YAML → defaults. Logging goes to stderr
with timestamps, levels, and module names, suitable for journald.

Continuous low-cost perception should remain separate from expensive AI reasoning.

```text
camera → local event/detection → interesting event?
  no  → continue locally
  yes → capture/select context → higher-level reasoning → action / speech / API
```

Future VisionProvider, SpeechToTextProvider, TextToSpeechProvider, AgentProvider, EventBus,
and ToolExecutor belong behind adapters when those phases are implemented. Phase 0 adds no
provider SDKs or speculative interface layers. Future tool execution must use explicit
permissions independent of natural-language reasoning; never execute arbitrary model shell
output. Captured media remains local and is sensitive data.

The web controller serializes camera operations and runtime startup with a camera lock,
and uses a separate audio lock so speech remains available during video.
Controls invoke adapters directly instead of invoking shell commands. The embedded runtime
uses a stop event and leaves signal handling and logging lifecycle to the web server.
HTTP actions return structured results, a JPEG capture, or an error with a non-success status.
The browser uses textContent for device data and errors, and revokes old capture object URLs.

Live video is an explicitly started MJPEG feed. One worker owns the camera and publishes
its latest resized JPEG through a condition variable, allowing multiple HTTP viewers
without opening multiple camera handles. Hiding preview ends viewer responses and releases
the camera if Sentry is disarmed; shutdown always releases it. Snapshots reuse the latest
captured frame; status never reopens an active camera.
The main UI is at `/`; hardware-test controls are at `/tests`.

Speech synthesis passes validated text to local Piper or eSpeak NG through stdin and writes
a temporary WAV. Installed Piper voices supply neural English/Italian speech; other voices
can fall back to eSpeak when engine=auto. Model downloads are explicit setup operations and
runtime speech stays local. A complete-sample check rejects truncated WAVs before playback.
FFmpeg converts speech to 48 kHz stereo with configurable leading/trailing silence;
PipeWire playback uses a configurable buffer to protect short Bluetooth utterances. Text is never interpreted as a
shell command. Language/rate defaults live in the speech configuration section.

Phone push-to-talk captures only after explicit microphone permission and a held button.
An AudioWorklet sends ordered 100 ms mono PCM16 packets over same-origin HTTPS requests.
The controller gives each press an unguessable session token and holds the shared audio lock
until the continuous raw-audio player exits. A bounded queue absorbs short network jitter;
disconnect, cancellation, a 60-second limit, or three seconds without data end the session.
Audio is streamed through stdin without retaining recordings. HTTP and optional HTTPS listeners
share one NodeControls instance, including its camera, audio locks, and shutdown lifecycle.

Object detection uses OpenCV DNN with a pinned YOLOX Nano ONNX model. Its inference worker
shares camera frames through a single replaceable slot, so processing cannot build a backlog
or block video capture. Letterboxing, confidence filtering, class-wise NMS, normalized boxes,
and annotation live in `vision/detection.py`. Camera ownership is shared by preview and Sentry.
Stopping preview retains capture/inference while Sentry is armed. Otherwise capture and
inference workers stop. Disabling detection clears in-flight results and is rejected while
Sentry requires inference. Failed inference leaves plain preview available and disarms Sentry.

VoiceEffects validates presets, pitch, and volume for both synthesis and live audio. TTS
applies pitch before its playback padding; PTT optionally pipes raw PCM through one persistent
FFmpeg rubberband process into its audio player. Cancellation/shutdown reaps both processes.
Pitch preserves tempo, and per-request volume settings do not mutate the shared configuration.

Sentry evaluates completed inference results through a short callback, not by polling old
overlay boxes. Rule state contains consecutive hits, a presence latch, observed-absence time,
and last-trigger time. Confirmed appearances enqueue bounded action jobs, or log intended
actions in test mode. A separate worker executes speech under the shared audio lock or a
saved SSH command with a timeout. Stale jobs expire; disarm/fault signals cancel work and
discard the queue. Commands are fixed configuration strings, never assembled from image data.

Telegram messages use the same action queue and appearance rules, with shared bot/chat
settings. A short-lived Python child submits one plain-text HTTPS request to Telegram;
credentials enter through stdin. The parent enforces a five-second total deadline and
cancels/reaps the child on disarm. No retries are made because an interrupted request may
already have delivered. Provider errors are sanitized to avoid logging token-bearing URLs.
The private saved config retains the token; API responses omit it, blank updates preserve
it, and an explicit clear flag removes it. Test mode makes no Telegram requests.

The Sentry editor is at `/sentry`. Typed configuration validates references and is atomically
persisted in a private local file with optimistic revision checks. Arming is never persisted.
The event ring holds 200 structured entries; normal logging carries events to the server log.
Preview rendering depends on viewers and is skipped entirely when hidden. Headless capture
requests smaller frames at the configured detection rate; snapshots encode only on demand.

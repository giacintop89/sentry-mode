# Sentry Node — Repository Bootstrap Specification

## 1. Purpose

Bootstrap a new repository named **`sentry-node`** for a Raspberry Pi 5 based physical AI node using:

- Logitech StreamCam for video and microphone input
- Bluetooth speaker for audio output
- Raspberry Pi 5 as the local runtime
- Python as the primary application language
- Local-first processing where practical
- Optional remote/cloud AI providers behind clean interfaces

The bootstrap should create a **clean, production-oriented foundation** without prematurely implementing the full vision assistant.

The initial repository must be able to:

1. detect and open the camera;
2. enumerate and test audio input/output devices;
3. play a test sound through the configured Bluetooth speaker;
4. capture a frame from the camera;
5. expose hardware status through a small CLI;
6. load configuration from files/environment variables;
7. run basic automated tests;
8. run as a systemd service later without restructuring the project.

---

## 2. Target Platform

Primary deployment target:

- Raspberry Pi 5
- Raspberry Pi OS 64-bit
- Headless operation
- Python 3.11+
- Logitech StreamCam connected through USB
- Bluetooth speaker paired at OS level
- Network access available but not required for basic hardware tests

Development on Linux/macOS should remain possible where hardware-dependent features are mocked or disabled.

---

## 3. Repository Name

```text
sentry-node
```

Suggested Git remote:

```text
git@github.com:<owner>/sentry-node.git
```

Do not hard-code a GitHub username into application code.

---

## 4. Design Principles

The bootstrap must follow these rules:

- **Headless first**
- **Local first**
- Hardware access isolated behind interfaces/adapters
- No direct AI-provider calls inside core logic
- No hard-coded device IDs
- No hard-coded credentials
- Graceful degradation when camera, microphone, speaker, or network are unavailable
- Structured logging
- Configuration-driven behavior
- Easy to run manually and as a service
- Minimal dependencies at bootstrap stage
- Hardware tests must be explicitly invoked, not run as normal unit tests

---

## 5. Suggested Project Structure

Create this structure:

```text
sentry-node/
├── README.md
├── pyproject.toml
├── .gitignore
├── .env.example
├── LICENSE
├── Makefile
├── config/
│   └── sentry-node.example.yaml
├── docs/
│   ├── architecture.md
│   ├── raspberry-pi-setup.md
│   └── roadmap.md
├── scripts/
│   ├── bootstrap_pi.sh
│   ├── detect_camera.sh
│   ├── detect_audio.sh
│   └── install_service.sh
├── systemd/
│   └── sentry-node.service
├── src/
│   └── sentry_node/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── logging_config.py
│       ├── app.py
│       ├── hardware/
│       │   ├── __init__.py
│       │   ├── camera.py
│       │   ├── microphone.py
│       │   ├── speaker.py
│       │   └── status.py
│       ├── audio/
│       │   ├── __init__.py
│       │   └── playback.py
│       ├── vision/
│       │   ├── __init__.py
│       │   └── capture.py
│       └── core/
│           ├── __init__.py
│           ├── models.py
│           └── errors.py
└── tests/
    ├── unit/
    │   ├── test_config.py
    │   └── test_status.py
    └── hardware/
        ├── test_camera.py
        └── test_audio.py
```

Do not create unnecessary abstraction layers beyond this unless justified.

---

## 6. Python Packaging

Use `pyproject.toml`.

Preferred tooling:

- package manager: standard `pip` + virtual environment initially
- tests: `pytest`
- linting: `ruff`
- formatting: `ruff format`
- typing: `mypy` or `pyright` if convenient

Application package:

```text
sentry_node
```

Expose a CLI command:

```bash
sentry-node
```

Equivalent fallback:

```bash
python -m sentry_node
```

---

## 7. Initial Dependencies

Keep bootstrap dependencies small.

Suggested runtime dependencies:

```text
opencv-python-headless
PyYAML
pydantic
pydantic-settings
```

Optional audio dependency only if required by the selected implementation.

Prefer interacting with Linux audio through PipeWire/PulseAudio command-line utilities where that avoids unnecessary Python audio dependencies.

Development dependencies:

```text
pytest
pytest-cov
ruff
mypy
```

Use **`opencv-python-headless`**, not `opencv-python`, for the Raspberry Pi runtime. Sentry Node is headless-first and must not depend on OpenCV GUI/highgui functionality such as `cv2.imshow()`, `cv2.waitKey()`, or desktop window backends.

OpenCV is the default bootstrap vision library for:

- opening the configured camera stream;
- acquiring frames;
- image resizing/cropping/conversion;
- lightweight preprocessing and classical computer-vision operations;
- providing frames to future perception backends such as YOLO or remote vision models.

Prefer Linux/V4L2 tools for device discovery and capability inspection when they provide more reliable hardware information than probing camera indexes through OpenCV.

Do not add an LLM SDK, Whisper, YOLO, MQTT, FastAPI, or database dependency during bootstrap unless required by a basic test.

---

## 8. Configuration

Configuration must support:

1. YAML config file;
2. environment variable overrides;
3. safe defaults.

Example:

```yaml
node:
  name: sentry-node

camera:
  enabled: true
  device: 0
  width: 1920
  height: 1080
  fps: 30

microphone:
  enabled: true
  device: auto

speaker:
  enabled: true
  device: auto
  volume: 70

logging:
  level: INFO
```

Environment variables should use a common prefix:

```text
SENTRY_NODE_
```

Examples:

```text
SENTRY_NODE_LOGGING__LEVEL=DEBUG
SENTRY_NODE_CAMERA__DEVICE=0
```

No secrets should be placed in committed YAML files.

---

## 9. CLI Requirements

Implement a simple CLI with these commands.

### General status

```bash
sentry-node status
```

Output should include at minimum:

```text
Sentry Node
-----------
Camera:       OK / unavailable
Microphone:   OK / unavailable
Speaker:      OK / unavailable
Network:      OK / unavailable
```

### Camera information

```bash
sentry-node camera list
sentry-node camera test
sentry-node camera capture --output ./frame.jpg
```

`camera test` should:

- open the configured camera;
- capture several frames;
- print detected resolution and FPS where possible;
- exit with non-zero status on failure.

### Audio information

```bash
sentry-node audio list
sentry-node audio test-output
sentry-node audio test-input
```

`test-output` should play a short generated test tone or included test audio.

Avoid checking in large binary audio assets if a generated tone is sufficient.

### Configuration

```bash
sentry-node config show
sentry-node config validate
```

---

## 10. Camera Layer

Create a camera abstraction that prevents OpenCV calls from spreading throughout the project.

The initial camera implementation should use **OpenCV (`cv2`) for frame acquisition and image processing**, while keeping the rest of the application independent of OpenCV-specific types where practical. OpenCV is infrastructure for the vision pipeline, not the architecture itself.

The Pi runtime is headless. Do not use `cv2.imshow()`, GUI windows, or require a desktop session. Camera validation must work through CLI output and captured image files.

Minimum responsibilities:

```python
class Camera:
    def open(self) -> None: ...
    def close(self) -> None: ...
    def capture_frame(self): ...
    def is_available(self) -> bool: ...
```

The implementation should support device indexes such as:

```text
0
1
2
```

and, if practical, Linux paths such as:

```text
/dev/video0
```

Do not assume `/dev/video0` is always the StreamCam.

---

## 11. Audio Layer

Separate microphone and speaker detection.

On Raspberry Pi OS, prefer compatibility with the system's PipeWire/PulseAudio stack.

The system must tolerate:

- Bluetooth speaker disconnected;
- speaker reconnecting with a different runtime sink ID;
- StreamCam microphone not available;
- multiple audio devices connected.

Where possible identify devices by human-readable name instead of transient numeric index.

Useful OS-level inspection commands may include:

```bash
wpctl status
pactl list short sinks
pactl list short sources
arecord -l
```

The application must not depend on a single one of these commands without detecting its availability.

---

## 12. Hardware Status Model

Use a small structured status model, for example:

```python
@dataclass
class HardwareStatus:
    camera_available: bool
    microphone_available: bool
    speaker_available: bool
    network_available: bool
```

Allow additional diagnostic information without making the CLI dependent on raw subprocess output.

---

## 13. Logging

Use Python `logging`.

Default output:

```text
2026-09-13 12:34:56 INFO sentry_node.camera Camera opened: Logitech StreamCam
```

Requirements:

- timestamp;
- level;
- logger/module name;
- message;
- stdout/stderr suitable for `journalctl`;
- configurable log level.

Do not implement custom log rotation initially; systemd/journald can handle service logs.

---

## 14. systemd Preparation

Create:

```text
systemd/sentry-node.service
```

The initial service may run:

```bash
sentry-node run
```

The service must:

- restart on unexpected failure;
- start after networking is available;
- run as a normal non-root user;
- use an explicit working directory;
- support environment configuration;
- log through journald.

Do not enable the service automatically during repository bootstrap.

Provide an installation helper:

```bash
sudo ./scripts/install_service.sh
```

but require explicit user execution.

---

## 15. Raspberry Pi Bootstrap Script

Create:

```text
scripts/bootstrap_pi.sh
```

The script must be idempotent where practical.

Responsibilities:

1. verify Raspberry Pi / Debian-like environment;
2. install required OS packages;
3. create `.venv` if missing;
4. install Python package in editable mode;
5. print detected camera devices;
6. print detected audio devices;
7. run configuration validation;
8. print next commands.

Suggested OS packages:

```text
python3
python3-venv
python3-pip
v4l-utils
ffmpeg
pipewire-utils
pulseaudio-utils
alsa-utils
```

Only install packages that exist on the target Raspberry Pi OS release. Detect or gracefully skip unavailable packages rather than failing the entire bootstrap unnecessarily.

---

## 16. Camera Diagnostics Script

Create:

```text
scripts/detect_camera.sh
```

It should print useful information using tools such as:

```bash
v4l2-ctl --list-devices
v4l2-ctl --device=/dev/video0 --list-formats-ext
```

Do not assume the camera index.

The script must be read-only and make no persistent system changes.

---

## 17. Audio Diagnostics Script

Create:

```text
scripts/detect_audio.sh
```

It should display available:

- audio sinks;
- audio sources;
- current defaults;
- Bluetooth devices where visible through standard tools.

Do not modify the user's default devices in this diagnostic script.

---

## 18. Application Entry Point

Create a minimal `run` mode:

```bash
sentry-node run
```

At bootstrap stage it should:

1. load configuration;
2. initialize logging;
3. inspect hardware;
4. log node status;
5. remain alive until SIGINT/SIGTERM;
6. shut down cleanly.

It does **not** yet need to continuously stream video, transcribe speech, or call an AI model.

Example log:

```text
INFO sentry_node.app Starting Sentry Node
INFO sentry_node.camera Camera available
INFO sentry_node.microphone Microphone available
INFO sentry_node.speaker Speaker available
INFO sentry_node.app Sentry Node ready
```

---

## 19. Graceful Shutdown

The application must correctly handle:

```text
SIGINT
SIGTERM
```

On shutdown:

- release camera handles;
- stop audio playback/recording if active;
- terminate subprocesses;
- flush logs;
- exit cleanly.

This is important for later systemd operation.

---

## 20. Testing Strategy

### Unit tests

Run with:

```bash
pytest tests/unit
```

Unit tests must not require physical hardware.

Mock:

- camera interfaces;
- subprocess calls;
- audio device discovery;
- network state.

### Hardware tests

Keep hardware tests separate:

```bash
pytest tests/hardware -m hardware
```

Normal `pytest` should skip hardware tests unless explicitly requested.

Register the marker in `pyproject.toml`.

---

## 21. Makefile

Provide simple commands:

```bash
make setup
make test
make lint
make format
make status
make camera-test
make audio-test
```

Avoid wrapping every command unnecessarily; the Makefile is for common developer operations only.

---

## 22. `.gitignore`

At minimum ignore:

```text
.venv/
.env
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
logs/
captures/
.DS_Store
```

Keep empty runtime directories only if they are truly required.

---

## 23. `.env.example`

Provide documentation-only placeholders, for example:

```bash
SENTRY_NODE_LOGGING__LEVEL=INFO

# Future external AI provider configuration.
# Do not enable during bootstrap.
# SENTRY_NODE_AI__PROVIDER=
# SENTRY_NODE_AI__API_KEY=
```

Never commit real API keys.

---

## 24. README

The initial `README.md` must include:

1. project purpose;
2. current bootstrap-stage capabilities;
3. hardware prerequisites;
4. Raspberry Pi installation steps;
5. development installation steps;
6. CLI examples;
7. camera troubleshooting;
8. Bluetooth/audio troubleshooting;
9. running tests;
10. running manually;
11. systemd installation;
12. high-level roadmap.

Keep instructions copy/paste friendly.

---

## 25. Architecture Document

Create:

```text
docs/architecture.md
```

Initial architecture:

```text
                 ┌──────────────────────┐
                 │ Logitech StreamCam   │
                 └─────────┬────────────┘
                           │
                 ┌─────────▼────────────┐
                 │   Hardware adapters  │
                 │ camera / microphone │
                 └─────────┬────────────┘
                           │
                  ┌────────▼─────────┐
                  │    Sentry Node   │
                  │      Core        │
                  └────────┬─────────┘
                           │
          ┌────────────────┴────────────────┐
          │                                 │
 ┌────────▼────────┐               ┌────────▼────────┐
 │ OpenCV + future │               │ Future speech  │
 │ perception      │               │ / agent layer  │
 └─────────────────┘               └────────┬────────┘
                                            │
                                   ┌────────▼────────┐
                                   │ Bluetooth      │
                                   │ speaker        │
                                   └─────────────────┘
```

Document the rule:

> Continuous low-cost perception should remain separate from expensive AI reasoning.

Future processing should roughly follow:

```text
camera
  ↓
local event/detection
  ↓
interesting event?
  ├── no → continue locally
  └── yes
       ↓
   capture/select context
       ↓
   higher-level reasoning
       ↓
   action / speech / API
```

---

## 26. Roadmap Document

Create:

```text
docs/roadmap.md
```

Use these phases.

### Phase 0 — Bootstrap

- repository structure
- camera detection
- OpenCV headless frame acquisition and capture
- microphone detection
- speaker detection
- CLI
- configuration
- logging
- tests
- systemd preparation

### Phase 1 — Perception

- continuous camera capture loop
- frame-rate control
- local motion/person/object detection
- event generation
- snapshot/event storage

### Phase 2 — Speech

- microphone capture
- wake-word or push-to-talk mechanism
- speech-to-text adapter
- TTS adapter
- Bluetooth speaker output

### Phase 3 — Agent

- agent interface
- conversation/session state
- tool execution
- vision context requests
- safety boundaries for system commands

### Phase 4 — Integrations

Potential integrations:

- MQTT
- Telegram
- Home Assistant
- self-hosted applications
- local HTTP APIs
- Pi/server orchestration tools

### Phase 5 — Production Node

- event persistence
- health monitoring
- reconnect logic
- watchdog
- remote configuration
- OTA/update strategy
- metrics
- optional web dashboard

---

## 27. Future Interfaces — Do Not Fully Implement Yet

Reserve clean extension points for:

```text
VisionProvider
SpeechToTextProvider
TextToSpeechProvider
AgentProvider
EventBus
ToolExecutor
```

Example conceptual interfaces:

```python
class VisionProvider(Protocol):
    def analyze(self, frame, prompt: str) -> str: ...


class TextToSpeechProvider(Protocol):
    def speak(self, text: str) -> None: ...
```

These may initially have no concrete implementations.

Do not introduce provider SDKs during bootstrap.

---

## 28. Security Requirements

From the beginning:

- never commit secrets;
- do not run the application as root;
- do not expose a web server by default;
- do not execute arbitrary shell commands received from future AI components;
- separate agent/tool permissions from natural-language reasoning;
- treat camera and microphone data as sensitive local data;
- do not upload captured media anywhere unless explicitly configured later.

---

## 29. Git Workflow

Initial bootstrap should result in a clean first commit.

Suggested sequence:

```bash
git init
git branch -M main
git add .
git commit -m "chore: bootstrap sentry-node"
```

If a remote already exists, do not recreate the repository.

Development should normally happen on feature branches:

```bash
git switch -c feature/<name>
```

Keep `main` runnable.

---

## 30. Acceptance Criteria

Bootstrap is complete when all of the following work.

### Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### CLI

```bash
sentry-node --help
sentry-node config validate
sentry-node status
```

### Camera

On the Raspberry Pi with StreamCam attached:

```bash
sentry-node camera test
sentry-node camera capture --output /tmp/sentry-node-frame.jpg
```

The resulting image must be valid and non-empty.

### Audio

With the Bluetooth speaker connected:

```bash
sentry-node audio list
sentry-node audio test-output
```

The test sound must play through the configured/default speaker.

### Tests

```bash
pytest
ruff check .
```

must pass without requiring attached hardware.

### Service readiness

The application must start and stop cleanly through:

```bash
sentry-node run
```

and the provided systemd unit must be structurally valid.

---

# Codex Execution Instruction

Implement the repository bootstrap described above.

Work incrementally and keep the repository runnable after each meaningful step.

Before adding a dependency or abstraction, determine whether it is actually needed for **Phase 0**.

Prioritize:

1. reliable Raspberry Pi operation;
2. simple code;
3. clear hardware boundaries;
4. testability;
5. future extensibility without premature complexity.

Do not implement the complete AI assistant yet.

When finished:

1. run unit tests;
2. run linting;
3. show the resulting repository tree;
4. summarize implementation choices;
5. list the exact Raspberry Pi commands required for the first hardware smoke test;
6. report anything that requires physical verification on the Pi instead of claiming it works without testing it.

# Architecture

CLI / explicit read-only HTTP dashboard → validated settings → hardware adapters.
The idle application uses the same hardware status inspection as the CLI. Core dataclasses
carry device metadata and availability without exposing subprocess output.

```text
Logitech StreamCam → camera (OpenCV headless) → frame capture / future perception
                  → microphone (Pulse / ALSA) → future speech / agent layer
                                               ↓
                                     speaker (Pulse / ALSA) → Bluetooth speaker
```

OpenCV imports and types remain in the camera infrastructure; vision capture coordinates
acquisition without calling OpenCV. Audio subprocesses have bounded timeouts and are reaped
by subprocess.run even when timing out. Temporary test recordings and tones are deleted.
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

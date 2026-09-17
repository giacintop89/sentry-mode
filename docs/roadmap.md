# Roadmap

## Phase 0 — Bootstrap

Repository structure; camera discovery and headless OpenCV capture; microphone and speaker
detection; CLI; configuration; logging; mocked and opt-in hardware tests; systemd preparation.
Main video/speech UI on port 8083; hardware settings at `/hardware`, including the camera's
size, rate, pixel format and exposure.

## Phase 1 — Perception

Explicit live capture, shared MJPEG viewing, and a toggle for local OpenCV DNN object
detection are implemented. Sentry adds confirmed-appearance rules, ordered action sequences
with waits and explicit parallel groups, optional preview, an event log, and photo/video
captures kept on the node. Next: motion detection and tracking.

## Phase 2 — Speech

Microphone tests, local Kokoro/eSpeak NG synthesis, and live phone push-to-talk through
the node speaker are implemented, including Demon/Chipmunk/custom pitch and volume controls.
Phone microphone access uses optional local HTTPS.
Next: wake-word detection, speech-to-text, and additional TTS adapters.

## Phase 3 — Agent

Agent interface, conversation/session state, tool execution, requested vision context, and
safety boundaries for system commands.

## Phase 4 — Integrations

Saved SSH command actions, Telegram notifications and local TTS announcements are available
in Sentry rules. Potential MQTT, Home Assistant, self-hosted applications, local HTTP APIs,
and Pi/server orchestration integrations.

## Phase 5 — Production Node

Camera reconnect after a device drops off the bus is implemented, and capture timing is
measurable from both surfaces. Next: event persistence, health monitoring, watchdog, remote
configuration, OTA/update strategy, metrics, and an optional expanded web dashboard.

## Phase 6 — Satellites

A second board reports its sensors to this hub over MQTT with mutual TLS, sends video and
sound through the hub's own media gateway, and watches for a known device over Bluetooth.
Rules are set off by a sensor, a limit, a sensor confirmed by a camera, a sound on a node
or a device arriving and leaving; a node's sources can be changed from the hub, and a
node's releases are installed, verified and rolled back from a manifest
([satellites](satellites.md), [release checklist](release-checklist.md)).

Not done: a long run with metrics over days, Wi-Fi presence with a real router adapter, a
USB camera driver on a node, and packaging for the hub itself.

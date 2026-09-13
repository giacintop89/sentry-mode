# Roadmap

## Phase 0 — Bootstrap

Repository structure; camera discovery and headless OpenCV capture; microphone and speaker
detection; CLI; configuration; logging; mocked and opt-in hardware tests; systemd preparation.
Explicit read-only dashboard on port 8083 added for local hosting.

## Phase 1 — Perception

Continuous capture loop, frame-rate control, local motion/person/object detection, event
generation, and snapshot/event storage.

## Phase 2 — Speech

Microphone capture, wake-word or push-to-talk, speech-to-text and TTS adapters, Bluetooth output.

## Phase 3 — Agent

Agent interface, conversation/session state, tool execution, requested vision context, and
safety boundaries for system commands.

## Phase 4 — Integrations

Potential MQTT, Telegram, Home Assistant, self-hosted applications, local HTTP APIs, and
Pi/server orchestration integrations.

## Phase 5 — Production Node

Event persistence, health monitoring, reconnect logic, watchdog, remote configuration,
OTA/update strategy, metrics, and an optional expanded web dashboard.

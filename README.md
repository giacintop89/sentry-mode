# Vision Node

Headless, local-first hardware foundation for a Raspberry Pi 5 physical AI node.
Phase 0 supports Logitech StreamCam capture, microphone/speaker discovery, short audio
tests, YAML/environment configuration, hardware status, an idle service runtime, and an
explicitly started web dashboard. No AI models, media uploads, or continuous recording.

## Raspberry Pi installation

Use Raspberry Pi OS 64-bit, Python 3.11+, a USB StreamCam, and a Bluetooth speaker paired
at OS level. Run from the normal account that owns the PipeWire/PulseAudio session:

```bash
./scripts/bootstrap_pi.sh
source .venv/bin/activate
cp config/vision-node.example.yaml config/vision-node.yaml
vision-node --config config/vision-node.yaml config validate
```

The helper installs available Debian OS packages using sudo. It does not pair devices,
change audio defaults, or enable services. See [Pi setup](docs/raspberry-pi-setup.md).

## Development installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
```

Linux/macOS development is supported with mocked hardware tests; audio command-line
backends target Linux. OpenCV uses the headless distribution and never opens GUI windows.

## CLI and first hardware smoke test

```bash
source .venv/bin/activate
./scripts/detect_camera.sh
./scripts/detect_audio.sh
vision-node camera list
vision-node audio list
vision-node --config config/vision-node.yaml config validate
vision-node --config config/vision-node.yaml status
vision-node --config config/vision-node.yaml camera test
vision-node --config config/vision-node.yaml camera capture --output /tmp/vision-node-frame.jpg
vision-node --config config/vision-node.yaml audio test-input
vision-node --config config/vision-node.yaml audio test-output
```

Inspect the JPEG and confirm the tone is audible through the intended speaker. A successful
recording checks sample capture, not microphone sound quality. Status reports a local IPv4
route rather than Internet connectivity. Missing hardware is reported as unavailable; an
explicit failed hardware test exits nonzero.

`python -m vision_node` is equivalent to `vision-node`. Pass `--config` before the command,
or set `VISION_NODE_CONFIG`. With neither, safe defaults are used. Environment variables
such as `VISION_NODE_CAMERA__DEVICE=/dev/video2` and `VISION_NODE_LOGGING__LEVEL=DEBUG`
override YAML. `.env` is not implicitly loaded by the CLI; export values yourself or use the
service's EnvironmentFile. `vision-node config show` prints the effective configuration.

## Camera troubleshooting

Run `./scripts/detect_camera.sh` to inspect V4L2 paths and supported modes. Prefer a stable
`/dev/v4l/by-id/...` path in YAML; the StreamCam is not necessarily `/dev/video0`. Choose a
supported resolution/FPS, check USB power/bandwidth and account access to the `video` group,
and stop other processes using the camera. OpenCV-reported FPS may differ from measured FPS.
Capture output directories must already exist.

## Bluetooth and audio troubleshooting

Run `./scripts/detect_audio.sh`, `wpctl status`, and `pactl list short sinks`. Pair and connect
with `bluetoothctl`, then select the OS default or configure the exact sink/source name or
unique description from `vision-node audio list`. Runtime numeric Pulse IDs are not stored.
Discovery retries on each operation and falls back to ALSA when Pulse is unavailable.
Pulse capture uses FFmpeg; output uses paplay. ALSA uses arecord/aplay. A missing backend
produces a clear error. Bluetooth audio typically requires a running user audio session;
see the Pi guide for service setup. `speaker.volume` controls test tone amplitude and does
not modify the OS sink volume.

## Runtime and dashboard

```bash
vision-node run                 # idle service runtime; Ctrl-C to stop
vision-node serve --port 8083   # read-only dashboard on http://localhost:8083
# Restrict the dashboard to this machine if desired:
vision-node serve --host 127.0.0.1 --port 8083
```

`run` handles SIGINT/SIGTERM and releases inspection resources. The dashboard serves `/`
and `/api/status`, caches hardware probes for ten seconds, and has no media/control endpoints.
It listens on all interfaces when explicitly started with `serve`; no web server starts in
`run`. Use it on a trusted local network.

## Tests and service

```bash
pytest tests/unit
pytest tests/hardware -m hardware  # explicitly exercises camera, recording, and audible playback
make lint
sudo ./scripts/install_service.sh
systemctl cat vision-node
sudo systemctl enable --now vision-node  # explicit opt-in after reviewing the unit
journalctl -u vision-node -f
sudo systemctl stop vision-node
```

Normal pytest deselects hardware tests. The installer renders the current repository path
and sudo caller into the unit, then reloads systemd without enabling it. Do not run the app
as root. No service is installed by development setup.

Next phases add local perception/events, speech adapters, agent interfaces, integrations,
and operational monitoring. See [architecture](docs/architecture.md) and [roadmap](docs/roadmap.md).

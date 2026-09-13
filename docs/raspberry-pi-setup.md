# Raspberry Pi setup

Use Raspberry Pi OS 64-bit on Pi 5 with adequate USB power. Plug in the StreamCam and clone
this repository into the normal user's home directory. Then run:

```bash
./scripts/bootstrap_pi.sh
source .venv/bin/activate
cp config/sentry-node.example.yaml config/sentry-node.yaml
./scripts/detect_camera.sh
./scripts/detect_audio.sh
```

Select supported camera modes and a stable camera path from V4L2 diagnostics. If permissions
require it, add your account to video/audio groups and log in again:

```bash
sudo usermod -aG video,audio "$USER"
```

Pair the speaker interactively with `bluetoothctl`: `power on`, `scan on`, `pair <address>`,
`trust <address>`, `connect <address>`, then `scan off` and `quit`. Run `wpctl status` or
`pactl list short sinks` to confirm the audio sink. Select a default with OS tools or place
the sink's stable name / unique human-readable description in YAML. No diagnostic script
changes audio defaults. ALSA fallback may not expose a Bluetooth speaker.

Run all smoke commands in the README. Verify a real image, microphone sound quality, and
audible output on the intended speaker. These cannot be established using mocked tests.

## Headless user audio and service

PipeWire/PulseAudio normally runs per user. For headless service audio, arrange a persistent
user session as appropriate for your OS release. If needed, explicitly enable lingering:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user status pipewire pipewire-pulse
sudo ./scripts/install_service.sh
systemctl cat sentry-node
sudo systemctl enable --now sentry-node
journalctl -u sentry-node -f
```

The installer configures XDG_RUNTIME_DIR and the user's Pulse socket. That does not itself
start PipeWire or guarantee Bluetooth reconnection at boot. Confirm audio after reboot.
The template uses /opt/sentry-node and a non-root sentry-node account; use the installer to
render your actual checkout and account. Environment values can be stored in the checkout's
ignored .env file; use SENTRY_NODE_CONFIG with an absolute YAML path. The service runs the
idle `run` command. To host the dashboard persistently, review a systemd override replacing
ExecStart with the absolute venv command `sentry-node serve --port 8083`.

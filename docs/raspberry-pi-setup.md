# Raspberry Pi setup

Use Raspberry Pi OS 64-bit on Pi 5 with adequate USB power. Plug in the StreamCam and clone
this repository into the normal user's home directory. Then run:

```bash
./scripts/bootstrap_pi.sh
source .venv/bin/activate
cp config/sentry-mode.example.yaml config/sentry-mode.yaml
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
systemctl cat sentry-mode sentry-mode-web
sudo systemctl enable --now sentry-mode
sudo systemctl enable --now sentry-mode-web
journalctl -u sentry-mode-web -f
```

The installer configures XDG_RUNTIME_DIR and the user's Pulse socket. That does not itself
start PipeWire or guarantee Bluetooth reconnection at boot. Confirm audio after reboot.
The template uses /opt/sentry-mode and a non-root sentry-mode account; use the installer to
render your actual checkout and account. Environment values can be stored in the checkout's
ignored .env file; use SENTRY_MODE_CONFIG with an absolute YAML path.

Two units are installed and neither is enabled for you. `sentry-mode` runs the idle `run`
command; `sentry-mode-web` hosts the dashboard, serving the phone microphone view from the
local certificate in `.local/tls`. It binds plain HTTP 8083 to 127.0.0.1 and exposes only
HTTPS 8443 to the network, so nothing reaches the controls in clear text and a browser on
the node itself still uses `http://127.0.0.1:8083`. Drop `--https-host` from the unit's
`ExecStart` to serve both listeners on every interface. Most setups need only
`sentry-mode-web`; enabling both means the idle unit also probes the camera and audio
devices once at start. Create the certificate before installing:

```bash
python3 scripts/setup_phone_https.py 192.168.11.240 pi5 pi5.local
sudo ./scripts/install_service.sh
```

Without `.local/tls/server.crt`, `server.key`, and `ca.crt`, the installer says so and
renders the dashboard unit as loopback HTTP only, reachable from the node alone; rerun it
after creating them to pick up HTTPS. The
certificate and key stay readable only by the account the units run as. Change ports or
add flags with `sudo systemctl edit sentry-mode-web` and a full `ExecStart=` override, or
edit `systemd/sentry-mode-web.service` in the checkout and rerun the installer.

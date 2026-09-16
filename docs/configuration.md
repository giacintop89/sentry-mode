# Configuration

Configuration comes from a YAML file, overridden by environment variables, with safe
defaults when neither is given.

```bash
sentry-mode --config config/sentry-mode.yaml config validate
sentry-mode --config config/sentry-mode.yaml config show     # effective configuration
```

Pass `--config` before the command or set `SENTRY_MODE_CONFIG`. `.env` is not loaded
implicitly by the CLI: export the values yourself, or use the service's `EnvironmentFile`.
`python -m sentry_mode` is equivalent to `sentry-mode`.

## Environment variables

Every setting has an environment name prefixed `SENTRY_MODE_`, with `__` for nesting:

```bash
SENTRY_MODE_CAMERA__DEVICE=/dev/video2
SENTRY_MODE_LOGGING__LEVEL=DEBUG
SENTRY_MODE_SPEECH__VOICE=it
SENTRY_MODE_SPEECH__RATE=175
SENTRY_MODE_WEB_FRAME_ORIGINS='["http://127.0.0.1:8092"]'
```

## Sections

- `node` — `name`, reported in status output.
- `camera` — `enabled`, `device`, `width`, `height`, `fps`, `fourcc`. Prefer a stable
  `/dev/v4l/by-id/...` device path. `fourcc` is the pixel format asked of the device,
  `MJPG` by default because the uncompressed alternative caps 1080p at a few frames per
  second over USB 2; empty keeps whatever the device offers first. `exposure` is the value
  to hold the device at, in V4L2's 100 microsecond unit, or `0` to leave the exposure
  automatic. All of these are editable from
  [`/hardware`](hardware-and-devices.md); see [camera and video](camera-and-video.md) for
  what they cost and [USB devices](usb-devices.md) for what the link allows.
- `microphone`, `speaker` — device names or descriptions from `sentry-mode audio list`;
  runtime numeric Pulse IDs are never stored. `speaker.volume` is the test tone amplitude
  and does not touch the OS sink. `speaker.pipewire_latency_ms` defaults to 250.
- `speech` — `voice`, `rate`, `engine` (`auto`, `kokoro`, `espeak`), `speakers`,
  `kokoro_directory`, `lead_in_ms`, `tail_ms`, `talk_gain_db`. `auto` speaks with Kokoro when
  it is installed and falls back to eSpeak NG when it is not. `talk_gain_db` (15 by default,
  0 to 30) is how much quieter-than-full-scale phone audio may be lifted before it is played
  ([push-to-talk](push-to-talk.md)); `0` plays the phone's own level.
- `detection` — startup enablement, model path, confidence threshold (0.45), maximum
  inference rate.
- `sentry` — initial rules, `detection_fps`, `test_mode`, `action_ttl_seconds`, SSH commands
  and Telegram settings. Used only until the editor writes its own state file.
- `satellites` — `enabled` (off by default), `journal_file`, `fault_policy` (`isolated` or
  `global`). With this off the node behaves exactly as it did before there were
  satellites, and needs none of the optional dependencies.
- `logging` — level (`DEBUG` … `CRITICAL`).
- `web_frame_origins` — up to 16 exact HTTP(S) origins allowed to embed the app, without
  paths; empty blocks embedding.

## State files

These are written by the app, not by hand, with private file permissions:

| File | Contents | Override |
|---|---|---|
| `.local/sentry.json` | Saved rules, SSH commands, Telegram settings | `sentry_state_file` |
| `.local/soundboard.json` | Up to 48 saved messages | `soundboard_file` |
| `.local/soundboard/` | One mp3 sample per saved message | `soundboard_directory` |
| `.local/captures/` | Photos, videos, recordings (newest 200) | `captures_directory` |
| `.local/sounds/` | The shared audio-file library | `sounds_directory` |
| `.local/tls/` | Local CA and server certificate for phone HTTPS | TLS CLI options |

`.local/sentry.json` takes precedence over the `sentry` YAML section, which only supplies
initial defaults. Arming is never persisted: a restart always leaves Sentry disarmed.

## Running as a service

```bash
sudo ./scripts/install_service.sh
systemctl cat sentry-mode
sudo systemctl enable --now sentry-mode
journalctl -u sentry-mode -f
```

The installer renders the current repository path and the sudo caller into the units and
reloads systemd without enabling anything. It creates `sentry-mode` (the idle `run`) and
`sentry-mode-web` (the dashboard on loopback HTTP 8083 and HTTPS 8443 for the network). Do
not run the app as root; development setup installs no service. Bluetooth audio generally
needs a running user audio session — see [Raspberry Pi setup](raspberry-pi-setup.md).

The dashboard reads its HTML, CSS and JavaScript once at startup, so restart the service
after changing them:

```bash
sudo systemctl restart sentry-mode-web
```

See also: [security model](security.md), [HTTP API](http-api.md).

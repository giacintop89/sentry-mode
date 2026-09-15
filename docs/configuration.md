# Configuration

Configuration comes from a YAML file, overridden by environment variables, with safe
defaults when neither is given.

```bash
sentry-node --config config/sentry-node.yaml config validate
sentry-node --config config/sentry-node.yaml config show     # effective configuration
```

Pass `--config` before the command or set `SENTRY_NODE_CONFIG`. `.env` is not loaded
implicitly by the CLI: export the values yourself, or use the service's `EnvironmentFile`.
`python -m sentry_node` is equivalent to `sentry-node`.

## Environment variables

Every setting has an environment name prefixed `SENTRY_NODE_`, with `__` for nesting:

```bash
SENTRY_NODE_CAMERA__DEVICE=/dev/video2
SENTRY_NODE_LOGGING__LEVEL=DEBUG
SENTRY_NODE_SPEECH__VOICE=it
SENTRY_NODE_SPEECH__RATE=175
SENTRY_NODE_WEB_FRAME_ORIGINS='["http://127.0.0.1:8092"]'
```

## Sections

- `node` — `name`, reported in status output.
- `camera` — `enabled`, `device`, `width`, `height`, `fps`. Prefer a stable
  `/dev/v4l/by-id/...` device path.
- `microphone`, `speaker` — device names or descriptions from `sentry-node audio list`;
  runtime numeric Pulse IDs are never stored. `speaker.volume` is the test tone amplitude
  and does not touch the OS sink. `speaker.pipewire_latency_ms` defaults to 250.
- `speech` — `voice`, `rate`, `engine` (`auto`, `piper`, `espeak`), `models`,
  `model_directory`, `lead_in_ms`, `tail_ms`.
- `detection` — startup enablement, model path, confidence threshold (0.45), maximum
  inference rate.
- `sentry` — initial rules, `detection_fps`, `test_mode`, `action_ttl_seconds`, SSH commands
  and Telegram settings. Used only until the editor writes its own state file.
- `logging` — level (`DEBUG` … `CRITICAL`).
- `web_frame_origins` — up to 16 exact HTTP(S) origins allowed to embed the app, without
  paths; empty blocks embedding.

## State files

These are written by the app, not by hand, with private file permissions:

| File | Contents | Override |
|---|---|---|
| `.local/sentry.json` | Saved rules, SSH commands, Telegram settings | `sentry_state_file` |
| `.local/soundboard.json` | Up to 48 saved messages | `soundboard_file` |
| `.local/captures/` | Photos, videos, recordings (newest 200) | `captures_directory` |
| `.local/sounds/` | The shared audio-file library | `sounds_directory` |
| `.local/tls/` | Local CA and server certificate for phone HTTPS | TLS CLI options |

`.local/sentry.json` takes precedence over the `sentry` YAML section, which only supplies
initial defaults. Arming is never persisted: a restart always leaves Sentry disarmed.

## Running as a service

```bash
sudo ./scripts/install_service.sh
systemctl cat sentry-node
sudo systemctl enable --now sentry-node
journalctl -u sentry-node -f
```

The installer renders the current repository path and the sudo caller into the units and
reloads systemd without enabling anything. It creates `sentry-node` (the idle `run`) and
`sentry-node-web` (the dashboard on loopback HTTP 8083 and HTTPS 8443 for the network). Do
not run the app as root; development setup installs no service. Bluetooth audio generally
needs a running user audio session — see [Raspberry Pi setup](raspberry-pi-setup.md).

The dashboard reads its HTML, CSS and JavaScript once at startup, so restart the service
after changing them:

```bash
sudo systemctl restart sentry-node-web
```

See also: [security model](security.md), [HTTP API](http-api.md).

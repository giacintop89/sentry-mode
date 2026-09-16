# Sentry Mode

Headless, local-first hardware foundation for a Raspberry Pi 5 physical AI node.
Phase 0 supports Logitech StreamCam capture, microphone/speaker discovery, short audio
tests, YAML/environment configuration, hardware status, an idle service runtime, and an
explicitly started web dashboard with live video, local text-to-speech, and phone push-to-talk.
Phone audio streams directly to the node speaker without saving recordings or using a cloud service.

This page is the whole manual. [`docs/`](docs/README.md) has one page per feature — camera,
detection, speech, push-to-talk, Sentry rules, the action sequencer, captures, Telegram, SSH
commands — plus the HTTP API, configuration and security references.

## Raspberry Pi installation

Use Raspberry Pi OS 64-bit, Python 3.11+, a USB StreamCam, and a Bluetooth speaker paired
at OS level. Run from the normal account that owns the PipeWire/PulseAudio session:

```bash
./scripts/bootstrap_pi.sh
source .venv/bin/activate
cp config/sentry-mode.example.yaml config/sentry-mode.yaml
sentry-mode --config config/sentry-mode.yaml config validate
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
sentry-mode camera list
sentry-mode audio list
sentry-mode --config config/sentry-mode.yaml config validate
sentry-mode --config config/sentry-mode.yaml status
sentry-mode --config config/sentry-mode.yaml camera test
sentry-mode --config config/sentry-mode.yaml camera capture --output /tmp/sentry-mode-frame.jpg
sentry-mode --config config/sentry-mode.yaml audio test-input
sentry-mode --config config/sentry-mode.yaml audio test-output
```

Inspect the JPEG and confirm the tone is audible through the intended speaker. A successful
recording checks sample capture, not microphone sound quality. Status reports a local IPv4
route rather than Internet connectivity. Missing hardware is reported as unavailable; an
explicit failed hardware test exits nonzero.

`python -m sentry_mode` is equivalent to `sentry-mode`. Pass `--config` before the command,
or set `SENTRY_MODE_CONFIG`. With neither, safe defaults are used. Environment variables
such as `SENTRY_MODE_CAMERA__DEVICE=/dev/video2` and `SENTRY_MODE_LOGGING__LEVEL=DEBUG`
override YAML. `.env` is not implicitly loaded by the CLI; export values yourself or use the
service's EnvironmentFile. `sentry-mode config show` prints the effective configuration.

## Camera troubleshooting

Run `./scripts/detect_camera.sh` to inspect V4L2 paths and supported modes. Prefer a stable
`/dev/v4l/by-id/...` path in YAML; the StreamCam is not necessarily `/dev/video0`. Choose a
supported resolution/FPS, check USB power/bandwidth and account access to the `video` group,
and stop other processes using the camera. OpenCV-reported FPS may differ from measured FPS.
Capture output directories must already exist.

## Bluetooth and audio troubleshooting

Run `./scripts/detect_audio.sh`, `wpctl status`, and `pactl list short sinks`. Pair and connect
with `bluetoothctl`, then select the OS default or configure the exact sink/source name or
unique description from `sentry-mode audio list`. Runtime numeric Pulse IDs are not stored.
Discovery retries on each operation: Pulse, then native PipeWire, then ALSA.
Pulse capture uses FFmpeg; output uses paplay. Native PipeWire uses pw-dump discovery and
pw-record/pw-play, even when pactl is missing. ALSA uses arecord/aplay and its configured
default device rather than assuming the first HDMI card. A missing backend
produces a clear error. Bluetooth audio typically requires a running user audio session;
see the Pi guide for service setup. `speaker.volume` controls test tone amplitude and does
not modify the OS sink volume.

## Runtime and dashboard

```bash
sentry-mode run                 # idle service runtime; Ctrl-C to stop
sentry-mode serve --port 8083   # hardware controls on http://localhost:8083
# Restrict the dashboard to this machine if desired:
sentry-mode serve --host 127.0.0.1 --port 8083
```

`--https-host` binds the HTTPS listener separately from `--host`, so plain HTTP can stay on
loopback while phones reach HTTPS across the network:

```bash
sentry-mode serve --host 127.0.0.1 --port 8083 --https-host 0.0.0.0 --https-port 8443 \
  --tls-cert .local/tls/server.crt --tls-key .local/tls/server.key --tls-ca .local/tls/ca.crt
```

Both commands are also packaged as systemd units. `scripts/install_service.sh` renders
`sentry-mode` (idle `run`) and `sentry-mode-web` (the dashboard on loopback HTTP 8083 and
HTTPS 8443 for the network) against your checkout and account, enabling neither; see the
Raspberry Pi guide.

`run` handles SIGINT/SIGTERM and releases inspection resources. The main page offers live MJPEG video and local text-to-speech through the node speaker.
**Hardware settings** at `/hardware` (`/tests` still resolves) offers the same operational controls as the CLI: refresh status, list/test/capture camera, list audio,
test microphone/speaker, show/validate effective configuration, and start/stop an idle runtime.
Its **Input and output devices** panel picks the camera, microphone, speaker, and speaker
volume from what the node reports; POST `/api/hardware` merges them into the YAML file named
by `SENTRY_MODE_CONFIG`, leaving every other setting untouched, and applies them to the
running dashboard without a restart. Without that variable there is no file to write and the
change lasts until the next restart. Changing the camera requires stopping video and
disarming Sentry first.
The UI matches Cyber Dashboard's DejaVu Sans Mono typography, green palette, angled banners,
and dark green modules. It supplies app navigation only; the dashboard owns the outer frame.
The mark in the navigation corner is green when the node answers and red when it does not, and
the whole mark fades slowly in and out while the node streams, meaning live preview is running
or someone is listening to the microphone or holding push-to-talk (`GET /api/streams`).
Reduced-motion settings keep it steady.
Video and Voice are separate views, and Sentry separates Rules, Integrations, and Event log.
The rule editor has Conditions and Actions tabs with persistent save controls; the Actions
tab is the rule's ordered list of steps, each of which closes into a one-line summary. **Test rule**,
beside Delete rule, runs the actions in the editor once on the node without saving the rule or
waiting for a detection; it is refused while armed, reports what ran in the event log, and runs
for real whether or not test mode is on, since test mode holds back detections, not buttons. On a wide screen with the height for it, long editors
scroll inside the available frame height; narrower or shorter screens — phones and tablets in
either orientation — scroll the page instead, stack the rule library above the editor, and give
the camera the page's width.
Drafts survive switching tabs. Direct links include `/#speech`, `/#push-to-talk`,
`/sentry#integrations`, and `/sentry#events`. Assets require no external CDN.

To embed the app, set `web_frame_origins` in YAML (or the JSON-list environment variable
`SENTRY_MODE_WEB_FRAME_ORIGINS`) to the dashboard's exact HTTP(S) origins, for example
`["http://127.0.0.1:8092"]`. Default configuration blocks embedding. The parent dashboard
must also list Sentry Mode as an allowed application. This does not grant the parent access
to control APIs. Phone microphone use inside a frame additionally requires a secure parent
and delegated microphone permission; the standalone HTTPS Voice view remains available.
Capture shows a preview and JPEG download; temporary files are deleted after each request.
Audio tests execute on the node, not on your browser's microphone or speakers. Configuration
show/validate do not edit files, matching the CLI. Start/stop controls only the runtime owned
by this dashboard, not another CLI process or the systemd service; stopping it leaves the
web UI available. The dashboard's runtime stops when the web server shuts down.

Live video uses one camera worker shared by connected viewers, capped at 10 FPS and 960 pixels
wide. **Start** and **Stop** it on the main page; video remains active until stopped or the
server shuts down. Stopping video hides preview; Sentry can retain the camera. Camera tests require stopping
video and disarming Sentry first. Captures reuse the active camera's latest frame. Preview
JPEGs are encoded only with viewers (plus one initial frame); hidden preview produces none.
Speaker actions use a separate lock and remain available while video is live.

**Record** beside Stop saves what the live preview is showing to the Sentry **Captures**
tab: an H.264 MP4 with node-microphone sound, up to 60 seconds. The button stays lit red
while recording; press it again (or stop video) to finish early and keep the recording; if the microphone is unavailable the
video is still saved without sound. POST `/api/video/record/start` and
`/api/video/record/stop` do the same, and `/api/video/status` reports the recording under
`recording`.

**Enable audio**, on the right of the video controls, listens to the node microphone in your
browser while the preview runs; it starts off by default and stops with the video or a hidden
tab.
GET `/api/audio/monitor` streams raw 16-bit mono PCM at 16 kHz from `pw-record`, which the
page schedules through the Web Audio API with about a fifth of a second of delay. A media
element buffers on its own schedule and drifts; scheduling each block keeps the sound close
to the picture. Two listeners can hear the node at once; a third is refused. Browsers keep
the page silent until you interact with it, so the first click starts playback.

**Object detection** beside the video controls toggles labeled boxes and confidence scores
for 80 common COCO categories. It starts off by default and can be changed during playback.
All viewers share the toggle. Install the lightweight, checksum-pinned model once with:

```bash
python3 scripts/setup_detection.py
```

Detection uses [YOLOX Nano](https://github.com/Megvii-BaseDetection/YOLOX) through OpenCV DNN
on the CPU, with no cloud calls or additional Python inference framework. A separate worker
examines the latest frame at up to two updates/second, avoiding a queue of old video frames.
Boxes can lag moving objects slightly and expire after 1.5 seconds; this is object detection,
not tracking or identification. Small/obscured objects and categories outside the model can
be missed. Video remains available if the detector fails. The `detection` YAML section sets
startup enablement, model path, confidence threshold (default 0.45), and maximum inference
rate. A missing model is reported when detection is enabled; runtime never downloads it.

### Sentry rules

Open **Sentry** from the navigation or visit `/sentry`. A starter person rule is provided:
70% confidence, three consecutive detections, ten seconds of observed absence before rearming,
and a 60-second cooldown. It announces “Hello. Please wait here.” when actions are enabled.
Sentry starts **disarmed**, with **test mode off**, so actions run as soon as it is armed. To use it:

1. Edit/save a rule: object category, confidence, count, confirmation count, cooldown, and
   optional rectangular region. Region coordinates are percentages of the image; an object's
   bounding-box center must be inside it.
2. Build the action sequence on the **Actions** tab. **Add step** appends a step — a photo,
   an audio recording, a video, an announcement, a built-in tune, a saved audio file, a
   saved SSH command, a Telegram message, or a **Wait** — and ↑ ↓ ✕ reorder or remove one.
   A rule holds up to 16 steps and the same kind may appear as often as needed, so a rule
   can speak, wait four seconds and speak again. Clicking a step's header closes it to a
   single line that says what it does; a rule of several steps opens closed, and a step
   with something missing opens itself when you save.
   Steps run from top to bottom. Tick **Together with the step above** to start a step at
   the same moment as the one before it; consecutive ticked steps form one group, and the
   sequence waits for the whole group before moving on. Only one step can use the speaker
   at a time, so grouped announcements, tunes and audio files still take turns. A step that
   fails is logged as `action_failed` and the sequence carries on with the next one.
   A **Wait** step (0.1–60 seconds) holds the sequence where it stands; the steps after it
   get that much longer before they expire, and photo, audio and video keep recording in
   the background while it waits.
   Announcements pick any voice installed on this node — the same list the Voice page
   offers, so two steps can answer in different voices — with speed, Demon/Chipmunk/custom
   pitch, and volume. A Studio voice needs a few seconds to synthesize before it speaks.
   Tunes
   support repeats (1-5), volume, and **Pitch (semitones)** from -24 to +24, which moves every
   note by the same interval and leaves the tune's length alone. Each action carries its own
   test button: **Test message** and **Test tune** play the editor's settings on the node
   speaker right away, while **Test send** and **Test command** run that single action
   through Sentry, so they are refused while armed and appear in the event log. Every test
   button runs for real, test mode included: test mode holds back what a detection would do,
   not what you asked for by pressing a button, so a Telegram test needs the credentials a
   logged-only rule can go without. They act on the first click, since each names what it
   does; **Test rule**, which can fire several actions at once, says what will run and acts
   on a second click. None of them saves the rule.
3. To rehearse first, tick **Test mode** and start Sentry. The switch covers every rule, so
   it saves itself the moment you tick it — no Save, no rule edit — though like the other
   settings it is refused while armed. Confirm `triggered` and `would_run` events in the
   event log.
4. Disarm, untick **Test mode**, and start again to execute actions.
5. Pause live video on the Video view to save work while monitoring continues. Disarm Sentry to stop rules;
   the camera is released when neither Sentry nor preview needs it.

**Take a picture** saves the newest camera frame as a JPEG. Set **Number of pictures** (1–20)
and **Time between pictures** (0.5–60 seconds) to take a series: the first is taken at once
and the rest follow in the background. **Record audio** saves the chosen length (1–60
seconds) of node-microphone sound as an AAC `.m4a` file. **Record a video** saves an H.264
MP4 of the chosen duration (1–60 seconds) at up to 1280 pixels wide and 10 frames/second;
while it records, the camera temporarily switches to that size and rate. **Record sound with
the video** (on by default) muxes microphone audio into the MP4; when the microphone is
unavailable the video is still saved, silent, and the event log says why. Photo, audio and
video steps record in the background, so the next step starts without waiting for them; put
a **Wait** after one to hold the sequence for as long as it records. Disarming stops a
recording early and keeps what was captured.
Encoding needs `ffmpeg` with libx264 (installed by the Pi bootstrap helper). Files are saved
in `.local/captures/` (override with `captures_directory` or
`SENTRY_MODE_CAPTURES_DIRECTORY`); only the newest 200 are kept. The **Captures** view lists
them to view, play, or delete; GET `/api/captures` lists them, `/captures/<name>` serves one
(with byte ranges for phone video players), and POST `/api/captures/delete` with
`{"name": ...}` removes one. Test mode logs photos, audio and video as `would_run` without
capturing anything.

Sentry remains active when the browser closes. With preview hidden, it requests camera
capture at up to 640 pixels wide and the selected detection rate (0.5–5 updates/second),
skips annotation/JPEG encoding, and retains just the latest frame. Supported camera modes
determine actual capture settings. Showing preview restores the normal configured camera
size/rate. The detection toggle cannot disable inference while Sentry is armed.

Only fresh inference results count toward confirmation. A continuously present matching
object triggers once. Observed absence rearms the rule, and cooldown still applies before
the next trigger. Missing/stale frames do not count as confirmation or absence. Multiple
people of the same category are treated as scene occupancy, not tracked identities.
Rules are evaluated locally; recordings and snapshots are not saved automatically.

Actions use a separate queue capped at 16 entries, one entry per step (a group of steps
that start together counts as one). Old actions expire after 15 seconds by
default, including announcements waiting for a busy PTT/speech operation; a step's budget
starts after the waits ahead of it, so a pause the rule asked for never makes the steps
behind it stale. Failed actions are logged without automatic retries, and the rest of the
sequence still runs. Disarming discards queued work and cancels local
playback/SSH/Telegram requests. Camera or detector failure disarms Sentry. An SSH command already executing
on the remote machine may continue after the local SSH process is stopped.

Configure SSH commands in the saved-command editor using the host/alias, optional user,
port, remote command, optional identity-file path on the node, and timeout. Set up key-based
access and verify host keys first as the normal user running Sentry Mode. Sentry uses
OpenSSH batch mode and strict host-key checking, as documented in
[ssh_config](https://man.openbsd.org/ssh_config); it does not accept passwords or automatically
trust unknown hosts. The saved command runs exactly as entered, with no detection-data
interpolation. Standard output is discarded; failures and bounded stderr appear in the log.
No SSH command is configured by default.

**Telegram:** In `/sentry` → **Integrations**, save the bot token obtained from
[BotFather](https://t.me/BotFather) and the destination numeric chat ID (including the minus
sign for groups), or a public channel `@username`. Start a conversation with your bot first,
or add it to the destination and grant permission to send. A numeric chat ID can be read
from `message.chat.id` in an update received by your bot through Telegram's
[getUpdates API](https://core.telegram.org/bots/api#getupdates). Use the app's HTTPS address
when entering the token. Add a **Send a Telegram message** step to the desired rules, enter
the message, optionally select silent delivery, and save each rule.

Messages use Telegram's [sendMessage API](https://core.telegram.org/bots/api#sendmessage)
over HTTPS, with plain text up to 4096 characters and no template substitution or image
uploads. The same confirmation, cooldown, absence rearming, queue limit, and action expiry
apply. Test mode logs `Telegram: ...` without contacting Telegram and works without
credentials. Live arming requires both a token and destination for enabled Telegram rules.
Requests have a five-second total deadline and no automatic retries. Delivery can be unknown
after a timeout or cancellation; a message already submitted may still arrive.

The bot token is stored in the private Sentry settings file, masked in general config
output, and omitted from Sentry API responses and event logs. A blank token field preserves
the saved token; the red **Remove bot token** button immediately clears it. Bot settings are shared
by Telegram actions across all rules. No bot or destination is configured by default.

The rule editor atomically saves `.local/sentry.json` (private file permissions). This file
takes precedence over the `sentry` YAML section, which supplies initial defaults; override
the path with `sentry_state_file` or `SENTRY_MODE_SENTRY_STATE_FILE`. Saving is disabled while
armed, and revision checks reject stale updates from other tabs. Restarting always leaves
Sentry disarmed. The UI displays the latest 200 events in the current server session;
event messages also go to the regular server log. A damaged saved configuration prevents
arming until a valid configuration is saved.

GET `/api/sentry/config` returns effective rules, SSH commands, Telegram settings (blank
`bot_token`), `telegram_token_configured`, supported categories, and a revision. POST the
edited `{ "config": {...}, "revision": N }` to the same endpoint to save. Optionally send
`"clear_telegram_token": true` at the top level to remove the saved token.
Each entry of a rule's `actions` list is one step, in the order it runs: a `wait` step has
`seconds`, and any step with `"with_previous": true` starts together with the step before it.
Saved configurations without the field keep running as the sequence they always were.
GET `/api/sentry/status` returns armed/test state, action status, and recent events. POST
`/api/sentry/start` and `/api/sentry/stop` arm/disarm. All POSTs require the same-origin
`X-Sentry-Mode-Control: 1` header; configuration saves also require JSON content type.
`/api/video/status` distinguishes preview `running` from `capture_running` and `monitoring`.

Text-to-speech speaks English and Italian with female voices only, through
[Kokoro-82M](https://github.com/thewh1teagle/kokoro-onnx) (**Studio** — Heart, Bella,
Nicole, Aoede, Kore, Sarah, Nova in American English, Emma and Isabella in British English,
Sara in Italian), with eSpeak NG as a basic fallback when Kokoro is not installed
(`sudo apt-get install -y espeak-ng`, included by the Pi bootstrap helper). Install it with:

```bash
./scripts/setup_speech.sh
```

This installs the optional `speech` Python extra and the one Kokoro model with its voice
pack (about 350 MB) into ignored `models/kokoro/`. Runtime synthesis stays offline. Restart
the server and reload the page. Choose a **Language**, then a **Voice** for it: one model
speaks every Studio voice, so the whole list is available as soon as it is downloaded. Each
language's configured speaker answers to the plain language id (`en`, `it`), the others are
named after it (`en-af_bella`), and eSpeak variants are `<language>+<variant>` (`it+f2`);
any of them works as `speech.voice`. Other languages and male voices are rejected. Enter up
to 1000 characters, choose a voice and speech rate, and click **Speak on node**. Speech uses
the same configured speaker/backend as the tone test. Temporary WAV files are deleted after
playback. Default voice/rate are configured through YAML `speech` or
`SENTRY_MODE_SPEECH__VOICE=it` / `SENTRY_MODE_SPEECH__RATE=175`; `speech.engine` is `auto` by
default and pins one engine when set to `kokoro` or `espeak`. `speech.speakers` names the
speaker each language answers with, and `speech.kokoro_directory` where the model lives. A
Studio voice is loaded per utterance, so it costs a few seconds before it speaks — worth it
where the voice is heard, not where a rule must answer instantly. No cloud service is used.

**Save message** keeps the text with its voice, rate and voice modification on the
**Soundboard**, where each saved message is a card: **Play** speaks it on the node again,
**Download** saves the sample as an mp3, **Preview** plays it through this browser's own
speaker, and × deletes it. Up to 48 messages are saved atomically to `.local/soundboard.json`
(private file permissions; override with `soundboard_file` or
`SENTRY_MODE_SOUNDBOARD_FILE`). Saving the same message twice keeps one card, and a card
cannot play while other audio is using the speaker.

A message is synthesized once, when it is saved, and kept as an mp3 sample in
`.local/soundboard/` (`soundboard_directory`). Pressing a card plays that sample, so a
Studio voice answers instantly instead of spending a few seconds synthesizing the same
words again. The sample is stored at full volume: pitch and volume are applied as it plays.
A card saved before this, or whose sample was lost, is rendered the first time it plays;
deleting a card deletes its sample. Preview serves that sample over
`GET /soundboard/<id>.mp3`, with the card's pitch applied first and the level brought up to
−16 LUFS, since the browser plays the file as it is and a phone has no amplifier behind it.

Speech WAVs are checked for complete sample data, converted before playback to 48 kHz stereo,
and padded with 1000 ms of leading silence and 750 ms of trailing silence to protect speech
when a Bluetooth sink starts/stops. Adjust `speech.lead_in_ms`, `speech.tail_ms`, and
`speaker.pipewire_latency_ms` (250 ms by default) if the speaker needs different buffering.
Long speech synthesis and playback are cancelled and reaped on server shutdown.

Both TTS and phone push-to-talk have independent **Voice modification** controls:
**Natural**, **Demon** (−7 semitones), **Chipmunk** (+7 semitones), and **Custom pitch**
(−12 to +12 semitones), plus volume from 0–100%. Presets preserve speaking speed; the
existing TTS speed slider still controls synthesis pace. Settings apply to the next utterance
or button press and do not alter OS volume or saved configuration. Live effect controls
are locked during a transmission. Non-natural pitch uses FFmpeg's `rubberband` filter;
the Pi's Debian FFmpeg package includes it. Other installations must provide that filter.
PTT effects add a small processing delay while retaining one continuous playback stream.

### Phone push-to-talk

Open the main page on a phone, tap **Enable mic**, allow access, then hold
**Hold to talk**. Your voice plays live through the configured node speaker with a short
buffering/Bluetooth delay. Release to finish; the microphone stays enabled for another press
until you turn it off or leave the page. Each press is limited to 60 seconds. Touch cancellation,
leaving the page, and connection loss stop transmission. The speaker is shared with speech
and hardware audio tests; concurrent actions report busy. Video can run during transmission.

**Record**, beside Hold to talk, records from the same phone microphone and saves the
message to the **Captures** view instead of playing it, up to 120 seconds. Like Record
on the Video view it lights up red while recording; press it again to stop and save. It uploads
the browser's recording (WebM, Ogg or MP4) to POST `/api/captures/message` as
`application/octet-stream`, which converts it to an AAC `.m4a` file of at most 5 MB.

Phone browsers require a trusted HTTPS connection for microphone access; a LAN HTTP address
cannot request it. The app can serve HTTP on 8083 and HTTPS on 8443 with shared controls:

```bash
python3 scripts/setup_phone_https.py 192.168.11.240 pi5 pi5.local
# Replace the IP/names above with this node's addresses. Then run:
sentry-mode serve --port 8083 --https-port 8443 \
  --tls-cert .local/tls/server.crt --tls-key .local/tls/server.key --tls-ca .local/tls/ca.crt
```

On the phone, visit the HTTP page and expand **Set up this phone** to download the public
local CA certificate and follow the iPhone/Android trust instructions. Where HTTP is bound to
loopback, fetch `/local-ca.crt` from the HTTPS address instead, accepting the browser warning
once, or copy `.local/tls/ca.crt` to the phone by other means. Then open the HTTPS
link and allow microphone access. Installing/trusting the CA is a one-time phone setup;
the helper does not change any device's trust settings. It stores private keys in ignored
`.local/tls/`, reuses the CA on reruns, and issues a one-year server certificate. Rerun it
and restart after changing the Pi address or before the server certificate expires
(`sudo systemctl restart sentry-mode-web` when the dashboard runs as a service).
Only the public CA is served at `/local-ca.crt`. A certificate already trusted by your
phone can instead be supplied through the same TLS options without `--tls-ca`.

Push-to-talk uses AudioWorklet with mono 48 kHz PCM16. No audio files are retained on the
server. One player process runs for the whole press. Leading/trailing padding uses the
speech settings capped at 1000/750 ms for live transmission. A bounded packet queue rejects
connections that fall behind; a three-second idle timeout releases abandoned sessions.
The browser limits each chunk to 100 ms and uploads them sequentially. Current Android Chrome
and iPhone Safari expose the required APIs over HTTPS; physical phone testing is still needed
for device-specific permission and audio behavior.

The server uses the same hardware adapters as the CLI, serializes camera/audio access, and
reports busy devices or failures in the UI. Status probes are cached for ten seconds.
Actions require POST with a same-origin control header; there are no arbitrary shell or
filesystem-path controls. It listens on all interfaces when explicitly started with `serve`;
no web server starts in `run`. This bootstrap has no login, so use a trusted local network
or bind to 127.0.0.1. Devices and configuration use GET `/api/camera/list`, `/api/audio/list`,
and `/api/config`; hardware actions use POST `/api/camera/test`, `/api/camera/capture`,
`/api/audio/test-input`, `/api/audio/test-output`, and `/api/config/validate`. Runtime uses
GET `/api/runtime` and POST `/api/runtime/start` or `/api/runtime/stop`. Video uses POST
`/api/video/start`, POST `/api/video/stop`, POST `/api/video/record/start`, POST
`/api/video/record/stop`, GET `/api/video/status`, and GET `/api/video` for MJPEG. Speech uses POST `/api/speech` with a JSON body containing `text`, optional `voice`, and
optional `rate`. POST actions require `X-Sentry-Mode-Control: 1`; speech also requires
`Content-Type: application/json`.
Phone audio uses POST `/api/talk/start`, `/api/talk/chunk`, `/api/talk/stop`, and
`/api/talk/cancel`. Start returns a session token, sample rate, and duration limit. Subsequent
requests require `X-Sentry-Mode-Talk` with that token. Chunks also require
`Content-Type: application/octet-stream` and consecutive `X-Audio-Sequence` values starting
at zero, with up to 9600 bytes per chunk. The same-origin control header is required on all
four endpoints. GET `/api/talk/config` supplies the HTTPS setup link settings.
TTS accepts an optional `effects` JSON object containing `preset`, `pitch` (semitones, used
with `custom`), and `volume`. `/api/talk/start` accepts that same effects object directly
as an optional JSON body. Presets are `natural`, `demon`, `chipmunk`, and `custom`.
POST `/api/video/detection` takes `{"enabled": true}` or `{"enabled": false}`; detection
state, current objects, and inference timing are included in GET `/api/video/status`.

## Tests and service

```bash
pytest tests/unit
pytest tests/hardware -m hardware  # explicitly exercises camera, recording, and audible playback
make lint
sudo ./scripts/install_service.sh
systemctl cat sentry-mode
sudo systemctl enable --now sentry-mode  # explicit opt-in after reviewing the unit
journalctl -u sentry-mode -f
sudo systemctl stop sentry-mode
```

Normal pytest deselects hardware tests. The installer renders the current repository path
and sudo caller into the unit, then reloads systemd without enabling it. Do not run the app
as root. No service is installed by development setup.

Next phases add local perception/events, speech adapters, agent interfaces, integrations,
and operational monitoring. See [architecture](docs/architecture.md) and [roadmap](docs/roadmap.md).

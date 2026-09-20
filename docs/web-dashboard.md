# Web dashboard

The dashboard is started explicitly and serves three pages: the main video/voice page, the
Sentry page at `/sentry`, and hardware settings at `/hardware` (`/tests` still resolves).

```bash
sentry-mode serve --port 8083                       # all interfaces
sentry-mode serve --host 127.0.0.1 --port 8083      # this machine only
```

No web server starts in `sentry-mode run`, which is the idle service runtime.

## Views

Video, Voice and Captures are views of their own in the navigation; Sentry separates Rules,
Integrations and Event log. The rule editor has Conditions and Actions tabs with save controls
that stay in view, and the Actions tab is the rule's ordered list of steps
([action sequencer](action-sequencer.md)).

When the hub has more than one camera, the Video view lists them with their state and how
old their newest picture is; the choice is remembered in the browser. Recording and the
quality settings stay with this node's own camera
([satellite cameras](satellites.md#watching-any-camera)).

Drafts survive switching tabs. Direct links include `/#speech`, `/#push-to-talk`,
`/sentry#integrations`, `/sentry#events`, `/sentry#captures` and `/sentry#actions`.

The mark in the navigation corner is green when the node answers and red when it does not,
and the whole mark fades slowly in and out while the node streams — live preview, someone
listening to the microphone, or push-to-talk (`GET /api/streams`). Reduced-motion settings
keep it steady.

## Hardware settings

`/hardware` offers the same operational controls as the CLI: choose the devices and the
camera's capture settings, list/test/capture camera, list audio, test microphone and
speaker, set the output sink's own level, show and validate the effective configuration, and
start or stop an idle runtime.
Audio tests execute on the node, not in the browser, and configuration show/validate never
edit files.

[Hardware and devices](hardware-and-devices.md) describes each control, what saving does,
and which changes apply to a running capture.

## Look and layout

The UI matches Cyber Dashboard's DejaVu Sans Mono typography, green palette, angled banners
and dark green modules, and supplies app navigation only: the surrounding frame belongs to
the host dashboard. Assets require no external CDN.

On a wide screen with the height for it, long editors scroll inside the available frame
height. Narrower or shorter screens — phones and tablets in either orientation — scroll the
page instead, stack the rule library above the editor, and give the camera the page's width.

## Embedding

Set `web_frame_origins` in YAML, or the JSON-list environment variable
`SENTRY_MODE_WEB_FRAME_ORIGINS`, to the dashboard's exact HTTP(S) origins, for example
`["http://127.0.0.1:8092"]`. Default configuration blocks embedding, and the parent
dashboard must also list Sentry Mode as an allowed application. This grants the parent no
access to control APIs.

A cross-origin frame answers `window.confirm()` with "no" without asking, so buttons that
act for real confirm in the page instead: the first click arms the button, the second runs
it, and it disarms itself after a few seconds. Phone microphone use inside a frame
additionally requires a secure parent and delegated microphone permission; the standalone
HTTPS Voice view remains available.

## When it cannot be reached

The command the Pi guide installs binds plain HTTP to loopback and only HTTPS to the
network, so `http://<node>:8083` from a phone is refused by design: from anywhere but the
node itself the dashboard lives at `https://<node>:8443`.

Ask the node first, in this order:

```bash
pgrep -af "sentry-mode serve"                  # a server, and the flags it was given
sudo ss -tlnp | grep -E ":8083|:8443"          # expect 127.0.0.1:8083 and 0.0.0.0:8443
curl -sk -o /dev/null -w '%{http_code}\n' "https://$(hostname -I | awk '{print $1}'):8443/"
openssl x509 -in .local/tls/server.crt -noout -dates -ext subjectAltName
sudo nft list ruleset; sudo iptables -S        # usually nothing on a Pi
```

A `200` from the third command means the server answers on the address the network uses,
and what is wrong lies between the client and the node rather than inside it.

| What you see | What it is |
|---|---|
| Refused or hanging on port 8083 from another device | `--host 127.0.0.1`. That port exists only on the node; the network uses 8443. |
| An empty reply or a reset connection on 8443 | An `http://` address against the TLS listener, which speaks only HTTPS. |
| A certificate warning — or an app that calls the node unreachable without showing one | The certificate is signed by the node's own CA. Trust it once per device; some apps report an untrusted certificate as a failure to connect rather than as a warning. |
| `unable to verify the first certificate` from `openssl s_client`, with nothing else wrong | Expected on a device that has not been given the CA. The node serves only the public half, at `/local-ca.crt`. |
| The browser refuses the name you typed although the same node answers its IP | The certificate lists the addresses it was issued for. Reissue it naming the one you use: `python3 scripts/setup_phone_https.py 192.168.11.240 pi5 pi5.local`. |
| Nothing answers, and other services on the same machine are silent too | The client is not on the node's network. |

Answering and doing its work are different questions, and the node reports the second one
itself:

```bash
curl -sk https://<node>:8443/api/runtime      # {"running": true, "error": null}
curl -sk https://<node>:8443/api/status       # which devices the node found
curl -sk https://<node>:8443/api/satellites   # "broker": "connected" once satellites are on
```

A page that loads while `/api/status` reports `camera_available: false` is a device
problem, not a network one — a camera knocked off a bus in protection looks exactly like
this ([USB devices](usb-devices.md)).

See also: [hardware and devices](hardware-and-devices.md), [HTTP API](http-api.md),
[security model](security.md).

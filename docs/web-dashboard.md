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

See also: [hardware and devices](hardware-and-devices.md), [HTTP API](http-api.md),
[security model](security.md).

# Live audio monitor

**Enable audio**, to the right of the video controls, plays the node microphone in the
browser while the preview runs. It starts off by default and stops with the video or a
hidden tab.

## How it sounds close to live

GET `/api/audio/monitor` streams raw 16-bit mono PCM at 16 kHz from `pw-record`. The page
schedules each block through the Web Audio API with about a fifth of a second of lead, and
rejoins the stream when it falls behind or drifts too far ahead after a stall. A media
element would buffer on its own schedule and drift away from the picture instead.

An odd byte count would split a sample, so a stray byte is carried into the next block.

## Limits

Two listeners can hear the node at once; a third is refused. Browsers keep a page silent
until the visitor interacts with it, so the first click starts playback — the page says so
when that is what is holding the sound back. Listening counts as streaming, so the mark in
the navigation corner fades while someone is listening (`GET /api/streams`).

See also: [camera and live video](camera-and-video.md), [captures](captures.md).

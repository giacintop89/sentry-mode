# Sentry Node documentation

The [README](../README.md) is the single-page manual: installation, troubleshooting and
every option in one place. The pages here take one feature each and describe how it behaves
and where its limits are.

## Features

- [Camera and live video](camera-and-video.md) — capture, preview, recording, camera sharing.
- [Object detection](object-detection.md) — YOLOX Nano through OpenCV DNN, on the node.
- [Live audio monitor](live-audio-monitor.md) — hearing the node microphone in the browser.
- [Text to speech](text-to-speech.md) — local voices, soundboard, voice modification.
- [Phone push-to-talk](push-to-talk.md) — speaking through the node speaker from a phone.
- [Sentry rules](sentry-rules.md) — confirmed appearances, arming, test mode, event log.
- [Action sequencer](action-sequencer.md) — the ordered steps a rule runs when it triggers.
- [Tunes and audio files](tunes-and-audio-files.md) — built-in tunes and the uploaded library.
- [Captures](captures.md) — photos, videos, recordings and how they are stored.
- [Telegram](telegram.md) — bot messages from a rule.
- [SSH commands](ssh-commands.md) — saved remote commands from a rule.
- [Web dashboard](web-dashboard.md) — views, navigation, embedding, small screens.

## Reference

- [HTTP API](http-api.md) — every endpoint, with the headers they require.
- [Configuration](configuration.md) — YAML, environment variables, state files, services.
- [Security model](security.md) — what is exposed, what is stored, what is refused.
- [Architecture](architecture.md) — how the parts fit together.
- [Raspberry Pi setup](raspberry-pi-setup.md) — hardware, audio session, service install.
- [Roadmap](roadmap.md) — what is implemented and what comes next.

## Plans

Dated design documents, kept as written; they describe proposals, not current behavior.

- [Low-latency implementation plan](sentry-node-low-latency-implementation-plan.md)
- [Vision node self-centering functional plan](vision-node-self-centering-functional-plan.md)

# Text to speech

The node speaks English and Italian with female voices, synthesized locally. No cloud
service is used.

## Voices

Local [Piper](https://github.com/rhasspy/piper) neural voices are used when installed, with
eSpeak NG as a basic fallback (`sudo apt-get install -y espeak-ng`, included by the Pi
bootstrap helper). Install the natural female voices — Lessac, Amy and Kristin in US
English; Alba, Cori and Jenny in British English; Paola and Serena in Italian, the two
female Italian Piper voices — with:

```bash
./scripts/setup_speech.sh
```

It installs the optional `speech` Python extra and downloads models into ignored
`models/piper/`. Restart the server and reload the page afterwards.

Choose a **Language**, then a **Voice** for it. Every Piper model in `models/piper/` named
like `en_GB-alba-medium` whose speaker is a known female voice appears as a speaker of its
language, marked **Natural**; other models are ignored. A language with no neural voice
offers eSpeak NG's Female 1, 2 and 3, marked **Basic**. Voice ids are the language (`it`),
`<language>-<speaker>` (`en-alba`) or `<language>+<variant>` (`it+f2`), and any of them works
as `speech.voice`. Other languages and male eSpeak variants are rejected.

## Speaking

Enter up to 1000 characters, choose a voice and speech rate, and press **Speak on node**.
Speech uses the same configured speaker and backend as the tone test, and temporary WAV
files are deleted after playback. POST `/api/speech` takes `text`, optional `voice`,
optional `rate` and an optional `effects` object.

Speech WAVs are checked for complete sample data, converted to 48 kHz stereo before
playback, and padded with 1000 ms of leading and 750 ms of trailing silence, which protects
speech when a Bluetooth sink starts and stops. Adjust `speech.lead_in_ms`, `speech.tail_ms`
and `speaker.pipewire_latency_ms` (250 ms by default) if the speaker needs different
buffering. Long synthesis and playback are cancelled and reaped on server shutdown.

## Voice modification

TTS and [push-to-talk](push-to-talk.md) each have their own controls: **Natural**, **Demon**
(−7 semitones), **Chipmunk** (+7 semitones) and **Custom pitch** (−12 to +12 semitones),
plus volume from 0–100%. Presets preserve speaking speed; the speed slider still controls
synthesis pace. Settings apply to the next utterance and change neither OS volume nor saved
configuration. Non-natural pitch uses FFmpeg's `rubberband` filter, which the Pi's Debian
FFmpeg package includes.

## Soundboard

**Save message** keeps the text with its voice, rate and voice modification on the
**Soundboard**, where each message is a card: press it to speak it again, × to delete it.
Up to 48 messages are saved atomically to `.local/soundboard.json` with private file
permissions (override with `soundboard_file` or `SENTRY_MODE_SOUNDBOARD_FILE`). Saving the
same message twice keeps one card, and a card cannot play while other audio holds the
speaker.

A rule's announcement step lists every voice installed on this node, the same list the
Voice page offers, so two steps of one rule can answer in different voices. The rule keeps
the voice id: a voice that is no longer installed stays in the step as a missing entry
rather than being replaced by another one.

A rule's announcement step can also copy a saved message — text, voice, speed and
modification — into the rule. The copy is what the rule keeps, so deleting the card never
changes the rule.

## Defaults

YAML `speech` or `SENTRY_MODE_SPEECH__VOICE=it` / `SENTRY_MODE_SPEECH__RATE=175` set the
defaults. `speech.engine` is `piper` to require neural voices, `espeak` to force basic
synthesis, or `auto` (default) to choose installed Piper models per language;
`speech.models` and `speech.model_directory` name what to load.

See also: [action sequencer](action-sequencer.md), [push-to-talk](push-to-talk.md).

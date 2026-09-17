# Satellite audio profile

**Status:** built and tested against a simulated capture; **not yet qualified on the board**, whose microphone is not wired
**Gate:** the PR-11 gate of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)
**Decision:** numbered blocks of 16 kHz mono PCM, through the same mutual-TLS gateway as video. No WebSocket, and no codec on the node.

## The pipeline

```text
I²S microphone (INMP441)
  -> arecord, raw S16_LE, 16 kHz, one channel, through the ALSA plug layer
  -> one capture per microphone, shared by the activity detector and any stream
  -> 100 ms blocks, each with a sequence number, its first sample and the node's clock
  -> the hub's media gateway, opened as kind "audio"
  -> put back in order, short gaps filled with silence
  -> each listener's own bounded queue: a browser, or ffmpeg reading a named pipe
```

Nothing is sent unless the hub holds a lease on the microphone: a browser listening, or a
rule recording. The activity detector, when a node has it switched on, sends a boolean
event and never sound.

## Why not a WebSocket

The plan named a WebSocket for audio. It was not used, for the reasons the video profile
chose its own gateway: the node already holds a certificate the hub has pinned, the
gateway already checks node, stream, source and a one-off token, and it already ends a
stream when the node is revoked or goes offline. A WebSocket would have been a second
listener with a second way of being trusted, for a stream whose only reader is the hub.
The one change to the gateway is that a stream is opened as `video` or `audio`, and a
connection claiming the other kind is refused.

## The block

The layout is `contracts/satellite/v1/audio.json`; both sides are tested against it.

| Field | Type | Meaning |
|---|---|---|
| `magic` | 4 bytes | `SMA1` |
| `version` | u8 | 1 |
| `flags` | u8 | bit 0: the node dropped samples before this block |
| `reserved` | u16 | 0 |
| `sequence` | u32 | from 0, per connection |
| `first_sample` | u64 | the capture's own sample count, which keeps running across reconnections |
| `captured_ns` | u64 | the node's wall clock when the block was read |
| `samples` | u16 | 1 to 3200; normally 1600 |

Little-endian, 30 bytes, followed by the samples. The hub drops a block that is not newer
than the last, fills a gap of up to one second with silence so recordings keep their
length, and ends the connection on anything that does not parse. The node then opens a
fresh one; nothing half-read is played.

## Time

A satellite's clock is not trusted to line sound up with anything, so a recording from a
satellite microphone is timed by when the blocks reached the hub, and its sidecar says so
with `sound_alignment: "hub_arrival"`. `captured_ns` is carried for diagnosis only.

## Cost on the Zero W

`audioop` is gone in Python 3.13, so the level is computed with `array` and a Python loop.
It is measured on every fourth sample, which is plenty to tell loud from quiet and a
quarter of the arithmetic: about 0.04 ms a block on the Pi 5, where the whole-block loop
took 0.14 ms. The Zero W has not run it yet; at twenty times slower it would still be well
under one per cent of a core.

With the INMP441's L/R pin grounded only the left channel carries sound, and the plug
layer's downmix to one channel halves it: levels read about 6 dB lower than the part
delivers. A threshold is set against the room anyway, so the capture stays mono.

## What a hub must not hear as a visitor

A hub playing a sound — a tune, a spoken message — can be heard by a satellite in the
same room. While the hub is playing, and for two seconds after, an `audio_event` rule
logs the event as skipped instead of running. That is the whole of the echo handling:
there is no cancellation, and a satellite far enough away to hear something else during
that time is ignored too.

## Not done

- `min_level` on `audio_event` is refused when arming: the event says that the node heard
  something loud by its own threshold, not how loud.
- No measurement on hardware: latency, CPU and the level of real sound all wait for the
  microphone to be wired.

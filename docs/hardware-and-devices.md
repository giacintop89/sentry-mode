# Hardware and devices

The `/hardware` view (`/tests` still resolves) is where the node's devices are chosen, its
capture settings are set, and each piece of hardware is exercised on its own. Every action
runs on the node, never in the browser, and the same operations exist in the CLI.

## Inputs and output

**Camera**, **Microphone**, **Speaker** and **Speaker volume** come from what the node
reports. "Session default" follows PipeWire; naming a device instead makes the node fail
loudly when that device is missing, which is usually what you want on a fixed installation.
Prefer a stable `/dev/v4l/by-id/...` camera path over an index — the camera is not
necessarily `/dev/video0`, and the number moves when devices re-enumerate.

`speaker.volume` is the test tone's amplitude and does not touch the OS sink.

Choosing a Bluetooth speakerphone's own microphone costs you its speaker. Bluetooth cannot
carry high-fidelity playback and a headset microphone at once, so asking for the microphone
drops the whole device into the headset profile — HSP/HFP, 16 kHz mono, quiet and dull, the
telephone link. Nothing in the node can make that loud; it is the wrong profile, not the
wrong volume. Use a separate microphone, such as the camera's, and leave the speaker on A2DP:

```bash
wpctl status                                    # the device id, and the profile in use
wpctl set-profile <device-id> <index>           # indexes come from the listing below
pw-dump <device-id> | grep -A2 EnumProfile      # a2dp-sink is the high-fidelity one
```

The device flips back on its own the moment anything opens that microphone, so change the
node's microphone as well as the profile, and make the other microphone the session default
with `wpctl set-default <source-id>`.

## Output level

Nothing the node plays can be louder than the sink it plays into, and a sink arrives at
whatever gain the session gave it — WirePlumber hands a newly seen device 0.064, and a
Bluetooth speaker reports its own knob when it connects. **Output level**, the slider in the
Audio panel, is that gain: it appears whenever the chosen speaker is a PipeWire sink, applies
the moment you let go, and belongs to the session rather than to this configuration, so it is
not written to the YAML file and the device keeps it until something else moves it.

It reads as a percentage of unity, which is the gain actually applied to the samples. 100% is
unmodified sound and the top of the control: PipeWire will go above it, but the samples are
simply multiplied, and everything already near full scale clips. Quiet phone audio is made
loud in the [push-to-talk](push-to-talk.md) path instead, where the lift stops at the peak.

The same from the shell, where the number means something different:

```bash
wpctl status                                   # the sink in use, and its volume
pw-dump <id> | grep channelVolumes             # the gain actually applied
wpctl set-volume <id> 1.0
```

Read the second one, not the first. `wpctl` counts in a cubic scale, so a sink showing `0.47`
is applying `0.47` cubed — about a tenth of the signal, a 20 dB loss, and 10% on the slider.

## Capture settings

The same form carries what the camera is asked for:

| Field | Meaning |
|---|---|
| Capture size | Width and height. It must be a pair the device offers, which `./scripts/detect_camera.sh` lists. |
| Frame rate | Frames per second asked of the device. What arrives is usually less; measure it rather than trusting it. |
| Pixel format | `MJPG`, `YUYV`, or the device's own default. Compressed is what lets a device reach its higher rates ([USB devices](usb-devices.md)). |
| Pinned exposure | The exposure to hold the device at, in V4L2's 100 microsecond unit, or `0` to leave it automatic. |

## Saving

POST `/api/hardware` merges the whole form into the YAML file named by `SENTRY_MODE_CONFIG`,
leaving every other setting untouched, and applies it to the running dashboard without a
restart. Without that variable there is no file to write and the change lasts until the next
restart; the panel says which case you are in.

Values are validated on the node by the same model that guards the YAML, so an impossible
rate or a three-character format is refused with 400 rather than reaching the device.

Size, rate, format and exposure apply to a running capture: it compares its settings each
pass and reopens the device on the next frame. Changing the **camera device** is the
exception — it requires stopping video and disarming Sentry first, and answers 409 until you
do.

## Tests

| Control | What it does |
|---|---|
| List cameras | The V4L2 paths the node can see. |
| Test camera | Opens the device, times it, and reports size, rate, negotiated format and a `latency` block ([camera and video](camera-and-video.md)). |
| Capture frame | A JPEG you can preview or download. While video is running it reuses the active camera's latest frame. |
| List audio devices | Microphones and speakers as the node reports them. |
| Test microphone | Records two seconds and discards them. |
| Play test tone | Plays on the node speaker; confirm audibility there. |
| Output level | The sink's own gain, applied at once. Hidden for a speaker that has none. |
| Show / validate configuration | The effective settings, and whether the loaded file is valid. Neither edits anything. |

Camera and audio tests take the node's hardware locks, so a test while another operation
holds the device answers 409 rather than queueing.

## Runtime

**Start runtime** and **Stop runtime** control the idle runtime owned by this dashboard —
the equivalent of `sentry-mode run` — and nothing else: not another CLI process, not the
systemd service. Stopping it leaves the dashboard available, and it stops when the web
server shuts down.

## The same from the CLI

```bash
sentry-mode camera list
sentry-mode camera test                 # size, format and the latency block
sentry-mode camera capture --output frame.jpg
sentry-mode audio list
sentry-mode audio test-input
sentry-mode audio test-output
sentry-mode config show
sentry-mode status
```

See also: [configuration](configuration.md), [HTTP API](http-api.md),
[USB devices](usb-devices.md), [web dashboard](web-dashboard.md).

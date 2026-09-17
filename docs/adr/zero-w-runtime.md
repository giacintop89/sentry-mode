# Zero W runtime: OS, packages and peripherals

**Status:** measured — 16 September 2026, on the board that will carry the first satellite
**Gate:** G0 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)
**Decision:** the original Zero W runs the agent profile; audio arrives over I²S, not USB

The original Zero W is ARM1176/ARMv6, 32-bit only, single core, and most wheels that install
elsewhere have no build for it. A successful run on a Pi 5 or a Zero 2 W qualifies nothing,
so `scripts/zero_w_qualify.py` reports the architecture first and refuses to imply anything
when it is not `armv6l`. Everything below came from that script over SSH.

## How to repeat this

```bash
scp scripts/zero_w_qualify.py pi0w:/tmp/
ssh pi0w 'python3 /tmp/zero_w_qualify.py --json /tmp/zero-w.json'
ssh pi0w 'python3 /tmp/zero_w_qualify.py --video 10'
ssh pi0w 'python3 /tmp/zero_w_qualify.py --broker <hub-ip>:8883 --ca /tmp/ca.crt'
```

## Measured

| Field | Value | Notes |
|---|---|---|
| Board model | Raspberry Pi Zero W Rev 1.1 | the original, not a Zero 2 W |
| `uname -m` | `armv6l` | |
| OS | Raspbian GNU/Linux 13 (trixie) | image checksum **not recorded**; it was flashed before this qualification |
| Kernel | 6.18.34+rpt-rpi-v6 | |
| Python | 3.13.5, 32-bit | `tomllib` present, well past the 3.11 the agent needs |
| RAM | 426 MiB total, 302 MiB available at idle | 426 rather than 512 after the GPU split |
| Free storage | 4563 MiB of 6978 MiB | |
| `vcgencmd get_throttled` | `0x0` | no under-voltage, so load figures mean something |
| Clock | NTP synchronised | remote events are not born `time_uncertain` |
| GPIO chips | `/dev/gpiochip0`, `/dev/gpiochip4` | |
| CSI camera | `imx219 [3280x2464 10-bit RGGB]` | already fitted with the narrow ribbon; cable part not recorded |
| USB audio capture | none | superseded: audio arrives over I²S, below |
| Bluetooth | present | |
| Wi-Fi | present | **signal at the install position not measured**; the board is on a bench |
| OpenSSL | 3.5.6 | |
| `paho-mqtt` | not installed; apt candidate `python3-paho-mqtt` **2.1.0** | the 2.x family the plan requires, as a Debian package |
| `sqlite3`, `tomllib` | available | |
| `rpicam-vid` | `/usr/bin/rpicam-vid` | |
| `ffmpeg` | not installed; apt candidate 7.1.5 | needed for the remux, not before PR-08 |
| TLS to the hub broker | not tested | no broker exists yet |
| Cold boot to sshd | about 60 s | the agent must reconnect with backoff, not assume a fast boot |

The two risks the plan called stopping conditions are both gone. Python is past 3.11, and
paho arrives from apt at 2.1.0, so nothing has to be compiled on ARMv6.

Because paho comes from the system, PR-02 has to choose and write down one of: an agent
virtualenv created with `--system-site-packages`, or paho installed inside the virtualenv.
Adding paths to `sys.path` until it starts is not the choice.

## Audio: I²S, not USB

The microphone is an INMP441, an I²S MEMS part on the GPIO header, so the plan's assumption
of a USB microphone on the OTG port does not apply here.

Enabled in `/boot/firmware/config.txt` (backup kept beside it as
`config.txt.before-inmp441`):

```
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
```

After a reboot the node reports `card 0: sndrpigooglevoi`, capturing **S32_LE, 2 channels,
48 kHz**, which is what the INMP441 needs: the part is a slave and the Pi supplies both
clocks. A two-second capture through `plughw` at 16 kHz mono returned 32000 frames at
**peak 0**, the correct reading with nothing wired, and the reference to compare against
once it is.

| INMP441 | Signal | BCM | Header pin |
|---|---|---|---|
| VDD | 3.3 V | — | 1 |
| GND | ground | — | 6 |
| SCK | bit clock | GPIO18 | 12 |
| WS | word select | GPIO19 | 35 |
| SD | data to the Pi | GPIO20 | 38 |
| L/R | channel select | — | to ground (39) for the left channel |

3.3 V, not 5 V: the header carries a 5 V rail, and neither the GPIO lines nor this part
tolerate it. With L/R grounded only the left channel carries signal, so a mono downmix
averages in a silent channel and costs 6 dB; capture two channels and take the left one
where the level matters.

Three consequences for the plans:

- **The OTG port stays free.** The functional plan (§3) kept `audio-sensor` apart from
  `camera-sensor` because a USB camera and a USB microphone would have to share one port.
  With CSI plus I²S this node can do both, and the two profiles collapse into one.
- **`audioop` is gone.** It was removed in Python 3.13, and this board runs 3.13.5, so the
  RMS and dBFS of AUD-07 cannot use it. `array` computes it in the standard library, but a
  per-sample Python loop on ARMv6 over continuous audio is a real cost to decide in PR-11,
  not a detail. PR-11 measures every fourth sample
  ([audio profile](satellite-audio-profile.md#cost-on-the-zero-w)).
- **The card registers with nothing attached.** Its presence proves the overlay loaded, not
  that a microphone exists. All-zero samples are "no signal", never "silence detected",
  exactly as SNS-03 requires for a sensor that cannot be read.

## Outcome

| Profile | State | Qualified on |
|---|---|---|
| `sensor-presence` | board qualified; **awaiting a PIR** to close G1 | Zero W Rev 1.1, 16 Sep 2026 |
| `camera-sensor` | board and camera qualified; **video transport pending** | imx219 detected and encoding, 16 Sep 2026 |
| `audio-sensor` | card registered and capture chain proved; **microphone not yet wired** | 16 Sep 2026 |

No profile is qualified for peripherals that were not attached during the test, and none of
these figures describes the node under load: nothing has streamed yet.

# Zero W runtime: OS, packages and peripherals

**Status:** unqualified — no Raspberry Pi Zero W has been measured yet
**Gate:** G0 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)
**Decision:** pending; this page is the form the measurement fills in

No satellite installation procedure may be published as verified, and no capability may
leave the `unqualified` state, until the table below is filled from a real Zero W. A
successful run on a Pi 5 or a Zero 2 W does not qualify anything: the original Zero W is
ARM1176/ARMv6, 32-bit only, single core, 512 MB, and most wheels that install elsewhere
have no build for it.

## How to fill this in

Copy the checker to the board and run it there; it installs nothing and changes nothing:

```bash
scp scripts/zero_w_qualify.py pi0:/tmp/
ssh pi0 'python3 /tmp/zero_w_qualify.py --json /tmp/zero-w.json'
ssh pi0 'python3 /tmp/zero_w_qualify.py --broker <hub-ip>:8883 --ca /tmp/ca.crt'
ssh pi0 'python3 /tmp/zero_w_qualify.py --video 10'
```

The script refuses to imply qualification when `machine` is not `armv6l`.

## Measurements to record

| Field | Value | Notes |
|---|---|---|
| Board model | | from `/proc/device-tree/model` |
| `uname -m` | | must be `armv6l` |
| OS image and checksum | | pin the exact image, never "latest" |
| Kernel | | |
| Python version and bitness | | 3.11+ needed for `tomllib` |
| RAM total / available at idle | | |
| Free storage | | |
| `vcgencmd get_throttled` | | under-voltage invalidates a load measurement |
| Clock synchronisation | | unsynchronised means every remote event is `time_uncertain` |
| GPIO chips present | | |
| CSI camera detected | | camera model and ribbon cable used |
| USB audio capture | | device, and whether an OTG adapter or hub was needed |
| Bluetooth adapter | | |
| Wi-Fi signal at the install position | | 2.4 GHz only |
| OpenSSL version | | |
| `paho-mqtt` version installed | | must arrive as a pure-Python wheel, no compilation |
| `sqlite3`, `tomllib` | | |
| `rpicam-vid` / `ffmpeg` present | | package versions |
| TLS connection to the hub broker | | including certificate SAN for the address used |

## Rules this measurement has to respect

- Record package versions and the image checksum. A qualification that names no version
  cannot be reproduced and does not qualify a later install.
- If any dependency needs a compilation that was not planned, stop that profile and pick a
  supported one. Do not build NumPy, Rust toolchains or AI packages on the node.
- A profile is qualified for the peripherals actually attached during the test. Camera plus
  USB microphone on the single OTG port is a separate qualification from either alone.
- Power supply and storage used during the test are part of the result. A marginal supply
  produces numbers that describe nothing.

## Outcome

| Profile | State | Qualified on |
|---|---|---|
| `camera-sensor` | unqualified | — |
| `sensor-presence` | unqualified | — |
| `audio-sensor` | unqualified | — |

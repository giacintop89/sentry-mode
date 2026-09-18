# Satellites

A satellite is a second, smaller computer with sensors on it, reporting to the node that
holds the rules. It is not a second Sentry: it reads, it says what it read, and it does
what a signed command allows. Every decision about the house stays on the hub.

This page is how to set one up. What the agent does once it is running is in
[what runs on the board](adr/satellite-agent.md); why the names look the way they do is in
[naming a source](adr/satellite-identity.md).

## Nothing happens until you switch it on

```yaml
satellites:
  enabled: true
  mqtt:
    host: 192.168.11.10      # the address the satellites use, not a loopback address
    port: 8883
```

With `enabled: false` — the default — the hub holds no broker client, opens no file and
starts no thread for any of this, and needs none of the optional dependencies. Install
them with the `satellites` extra when you are ready:

    pip install -e '.[satellites]'

## The authority, once

Every certificate here is issued by an authority you create and keep. Nothing is trusted
because a public CA signed it, and no node is trusted because its certificate is valid:
being valid is what gets it to the door.

    python scripts/satellite_admin.py init-ca
    python scripts/satellite_admin.py hub-cert --address 192.168.11.10

The address is the one the satellites actually connect to, and an IP address is fine — the
certificate is issued with it as a subject alternative name. There is no option anywhere in
this system for skipping verification; if the address changes, reissue the certificate.

The CA key stays on the hub. Only `ca.crt` is ever copied to a satellite.

## A node, one at a time

Make the key on the node when you can, so the private key never travels:

    # on the satellite
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -keyout node.key -out node.csr -subj "/CN=zero-entrance"

    # on the hub
    python scripts/satellite_admin.py sign --node zero-entrance --csr node.csr
    python scripts/satellite_admin.py register --node zero-entrance \
        --certificate /etc/sentry-mode/satellites/zero-entrance.crt --zone entrance
    python scripts/satellite_admin.py approve --node zero-entrance

Registering and approving are separate on purpose. A registered node is known; an approved
node is listened to, and approval pins the record to that exact certificate. Another
certificate from the same authority, for the same name, is still refused.

The name in the certificate is the node's identity. `sign` refuses to put a different one
in, and the broker is configured to use it as the username, so the topic a node writes to,
the name it authenticated as, and the name inside its messages all have to agree.

## What kind of machine a node is

Not every satellite is a small Linux computer. A microcontroller running the Sentry
firmware has the drivers that were compiled into it, a few kilobytes for its configuration,
no filesystem for a path to point into, and no camera. The hub has to know which kind it is
talking to before it sends anything, so a node is registered as one:

    python scripts/satellite_admin.py platforms
    python scripts/satellite_admin.py register --node pico-ingresso --platform pico-w-sensor

| Platform | Board | Drivers | Sources | Configuration | Streams |
| --- | --- | --- | --- | --- | --- |
| `linux-agent` | Pi Zero W or similar, running the Python agent | gpio, onewire, bme280, adc, csi, microphone, ble, board, dummy | 32 | 64 KiB | video, audio |
| `pico-w-sensor` | Pico W (RP2040) | gpio, onewire, adc, ble, microphone, board | 8 | 2 KiB | audio |
| `pico-2w-sensor` | Pico 2 W (RP2350) | gpio, onewire, adc, ble, microphone, board | 8 | 2 KiB | audio |
| `pico-wired` | Pico (RP2040), no radio, reached by a bridge | gpio, onewire, adc, microphone, board | 8 | 2 KiB | — |
| `pico-2-wired` | Pico 2 (RP2350), no radio, reached by a bridge | gpio, onewire, adc, microphone, board | 8 | 2 KiB | — |

`--platform` defaults to `linux-agent`, which is what every node registered before this
existed is, and what the page goes on showing them as. If a node turns out to be something
else, correct the record rather than re-registering it:

    python scripts/satellite_admin.py describe --node pico-ingresso --platform pico-2w-sensor

The two wired entries are the same chips without a radio. They are not a lesser Pico W:
they cannot reach a broker at all, and are reached over the USB cable that powers them by
`scripts/pico_bridge.py`, below. What changes is what they can be asked for — no Bluetooth,
so no `ble` source, and no live audio, because the hub hears a board's microphone over a
second TLS connection the board makes and a board with no radio cannot make one. A
microphone is still a source on them: saying something was loud happens where the sound is.

Every microcontroller entry is marked **experimental** everywhere it appears, on the page
and in the shell. A Pico 2 W has been run against this hub — provisioned, connected,
configured, and read from — and a Pico 2 has been run as a wired node, bridged, configured
and read from. A Pico W and a Pico have not, and no board has been through the whole
acceptance matrix. `firmware/pico/README.md` says which parts were executed on hardware and
which were not.

The limits in those two entries are the firmware's own, not a guess at what a chip can
stand: eight sources, because that is what the plan in `plan.h` holds, and about two
kilobytes of configuration, because it reaches the node inside one MQTT packet and is kept
in one flash sector. They are therefore the same for both boards, which run the same
firmware, and the RP2350 having more memory does not move them.

What a platform says is what is *possible*, never what is allowed. A node still has to be
approved, still has to be online, and still has to hold a lease before it may say anything;
the platform only decides what it can be asked for. Nothing here is taken from the node's
own word — there is no field on the wire in which a node declares what it supports.

A configuration is checked against the node's platform before it is sent, and refused with
a reason: a driver the firmware does not have, more sources than the board takes, an option
naming a file or an ALSA device on a board that has neither, a list too large to hold, a
source that names no pin when its driver is a pin, an option the driver has no place for
— `interval_seconds` on a wire that only speaks when it changes — or more than eight
settings on one source, which is what the firmware keeps room for beside a source's name
and kind. That last one is not a setting the node would drop: a ninth makes it give up on
the command it arrived in, so one crowded source would take the whole configuration with
it. A `ble` source is the one that can reach nine, having that many to choose from. The same word can belong
to one driver and not another, and is judged per driver: `device` on a 1-Wire source is a
probe on a bus rather than a sound card. The node checks all of it again and has the last
word; this is here so that a mistake the hub can see is an error on the page rather than a
round trip that can only come back `failed`.

`"enabled": false` keeps a source in the list without reading it, on a microcontroller as
on the agent: it is written down and reported, and it takes no pin and starts no driver. For the same reason, the card for a microcontroller shows only the board numbers
such a board can actually take — uptime, chip temperature, free memory — and no load
average, because a zero there would be a number nobody measured.

Beside those numbers the card counts how often a node has come back, and how often it came
back as a different boot. A link that keeps dropping and a board that keeps resetting both
look like a node that is there again, and they are not the same thing to whoever has to fix
it: the only thing that tells them apart is whether the boot id changed.

A microcontroller can say more than that: its chip records what reset it, and the firmware
reads that before it starts the watchdog again, so the heartbeat carries `power`,
`brownout`, `button`, `watchdog`, `software` or `debugger` and the card shows it in words.
A node that cannot tell leaves the field out and the card says nothing, rather than showing
the most reassuring of the answers it could have given; the Linux agent does not report it
at all. Which reset it was decides who has to look: a board that keeps coming back on the
watchdog has a fault in it, and one that keeps coming back on a brown-out has a power
supply problem that no change to this hub will fix.

## A board with no radio

A Pico or Pico 2 without a W has no radio, so it cannot be a satellite on its own. It can
still be one: plug it into a machine that already is, and run the bridge there.

    pip install -e '.[bridge]'
    python scripts/pico_bridge.py --config /etc/sentry-mode/pico-bridge.json

The file says which port is which node, and holds that node's own certificate:

```json
{
  "broker": {"host": "hub.local", "port": 8883, "ca": "/etc/sentry-mode/satellites/ca.crt"},
  "nodes": [
    {
      "node_id": "pico-cablato",
      "port": "/dev/serial/by-id/usb-Raspberry_Pi_Pico_2_E66...",
      "certificate": "/etc/sentry-mode/satellites/pico-cablato.crt",
      "key": "/etc/sentry-mode/pico-cablato.key"
    }
  ]
}
```

Issue that certificate and register the node exactly as for any other, with the platform
the board is: `pico-wired` or `pico-2-wired`. Use a path under `/dev/serial/by-id/` rather
than `/dev/ttyACM0`, which is whichever board enumerated first.

**The bridge is not a second rule engine.** It does not read an event, decide anything
about it, or change it. A frame's payload is published exactly as the board wrote it and a
command is handed over exactly as the hub sent it; the rules run on the hub.

**A board is trusted because somebody plugged it in and wrote it down.** A USB serial
number is a string a device chooses for itself, so the identity comes from the mapping
above and from nothing else. A board whose messages claim another node's name has those
messages refused and counted, never published — otherwise anything plugged into that
machine could speak as any satellite. The board says so from its side too: its retained
state carries `reached_by: bridge`, and the page and `/api/satellites` show it, because a
bridged node has a second thing that has to be running for it to be heard at all.

**Unplugged is offline.** The bridge connects as the node with the node's own goodbye as
its will, so a bridge that is killed reads on the hub exactly like a node that went away;
when the cable goes, the goodbye is published straight away. Either way the session ends,
the grant with it, and the node's sources go to unknown — nothing is reporting them.

What crosses the cable is framed: a magic, a version, what it carries, a counter, a length
and a CRC-32, written down in `contracts/satellite/v1/bridge.json`. The board's console is
framed too, in both directions, so a line of diagnostics can never be read as a message and
a message never lands in somebody's terminal. That is also how a wired board is
provisioned, since the cable is the only way in:

    python scripts/pico_bridge.py --config ... --console pico-cablato

Anything typed then goes to the board's console, and everything the board prints is logged
with its name in front of it. `status` on a wired board reports the cable rather than a
radio: whether a host has the port open, whether a bridge has said hello, and how many
frames were sent, read, thrown away or missed. Those last numbers are zero on a cable that
is working.

## The broker

Copy `deploy/satellites/mosquitto.conf.example` to `/etc/mosquitto/conf.d/`, and generate
the access list rather than writing it:

    python scripts/satellite_admin.py acl --out /etc/mosquitto/sentry-acl
    systemctl reload mosquitto

A node writes only under its own name and reads only its own commands. There is no topic a
satellite can publish to that another satellite reads.

Do not replace a broker configuration that is already there; the file above adds a listener
of its own. `deploy/satellites/firewall-policy.md` describes what the satellites should be
able to reach, which is one port on one machine and nothing else.

### What it refuses, watched rather than assumed

Four certificates were offered to this broker on 2026-09-17, on the machine this system
actually runs on, and what came back is what is written down here:

| What was offered | What the broker did |
|---|---|
| A certificate from another authority, for the right name | `tlsv1 alert unknown ca`, and the connection dropped |
| A genuine certificate that expired in February | `tlsv1 alert certificate expired`, and the connection dropped |
| A genuine certificate for a name that is registered nowhere | The handshake succeeded and **every** publish was refused: `Not authorized`, its own topic included |
| A node's genuine certificate, publishing under another node's name | The handshake succeeded, its own topic took the message and the other node's was refused |

The first two are the authority doing its work, and the second two are the access list
doing its work — which is why both exist. A name with no line in the access list can hold a
certificate this authority signed and still have nothing it may say, and that is exactly the
state a revoked node is left in once `acl` has been regenerated.

One thing that follows from this, and is worth saying plainly rather than leaving to be
discovered: on the broker's path the hub does not check which certificate a node presented.
It cannot — the broker terminates the TLS and hands the hub a name, not a certificate. The
fingerprint pinned at approval is checked where the hub terminates TLS itself, which today
is the media gateway. So a second certificate for the same name, signed by the same
authority, is accepted on this path: minting one needs the authority's own key, which is the
thing that has to be kept, and `ca.key` is `0600` for that reason. A revoked certificate is
not refused either — there is no CRL in this configuration, and revocation here is the
registry and the access list rather than a list of serial numbers.

## Taking a node away

    python scripts/satellite_admin.py revoke --node zero-entrance --reason "sold"
    python scripts/satellite_admin.py acl --out /etc/mosquitto/sentry-acl
    systemctl reload mosquitto

The hub stops believing the node the moment it is revoked: the session ends, the grants
end, its retained snapshot is erased and its sensors are marked unavailable. A hub that is
already running notices, because it asks the registry file again on every message rather
than trusting what it read at startup — which is the only thing standing between a revoked
node and the journal until the broker half is done. Do the broker half as well, and check
the connection is really gone — **reloading a broker does not close connections that are
already open.**

Giving it back is `approve` again, and one thing is worth knowing: a node that is still
connected stays offline until it says hello again, because a session begins at a `state`
message. Restarting the node — or the bridge carrying it — is what brings it back.

## What you will see

The hub reports the link and the nodes separately, because they fail separately. A broker
that is down is one fault, reported as `transport_unavailable`, not fifteen nodes that have
all mysteriously gone offline at the same instant. A node that is connected but quiet is
`stale` before it is `offline`.

Nothing a node reports is acted on before the hub has given it a grant, and a grant belongs
to one connection: after a reconnection the old one is worthless, which is what stops a
node that has been off the air from flooding the hub with news from an hour ago.

The subsystem is built when the dashboard starts, and only when `enabled` is true: a hub
with satellites off imports none of the code above. If the link cannot be started — no
certificate yet, the `satellites` extra not installed, a broker that has never been set
up — the dashboard still comes up with its own cameras and microphones working, and the
reason is reported with the rest of the satellite status. The subsystem is stopped with
the dashboard, which tells the nodes it is going before the socket closes.

A node's own devices are in the same inventory as everything a satellite reports:
`legacy-primary`, `legacy-microphone` and `legacy-speaker` are this board's camera,
microphone and speaker, named without a node prefix because they are not somewhere else.

## The journal

Every event a satellite sends is written to `satellites.store_path`, a SQLite file, and
only then acknowledged. If the hub dies before the write, the broker still holds the
message and delivers it again; if it dies after, the copy that arrives next is recognised
as a duplicate. A message the hub refuses on purpose is acknowledged too, so a malformed
event is not sent for ever.

Being in the journal does not mean being acted on. Only an event that is happening now
reaches a rule. Everything else is kept with its reason:

| Reason | What it means |
|---|---|
| `duplicate` | The same event again, usually a retry. |
| `id_reused`, `sequence_reused` | A different event wearing an identifier already used. A fault on the node. |
| `replayed` | Sent from the node's spool after a disconnection. History, not news. |
| `retained` | A copy the broker kept, delivered to a fresh subscriber. |
| `initial_state` | A sensor saying what it already was when it started. A baseline, not a change. |
| `out_of_order` | Older than something already seen from that source since it booted. |
| `expired` | Older than `limits.accept_within_seconds`. |
| `time_uncertain` | The board's clock is not synchronised, or is ahead of the hub's. The source stays in this state until it sends a new baseline. |
| `rate_limited` | Over the node's budget, or over everyone's. |
| `store_unavailable` | The journal could not be written. The event is **not** acknowledged. |
| `queue_full` | Written down, but the rules were too far behind to take it. Marked `dropped`. |

Heartbeats are not written down. The latest one from each node replaces the one before
and is shown with the node's status.

**How long a reading waited.** Every event carries the number of milliseconds the node held
it before it could be sent, measured on the node's own monotonic clock — so it does not
depend on the node and the hub agreeing about what time it is. The hub keeps the last one
and the worst of the last five minutes, and shows both under the node's `queue.waited` in
`/api/satellites`:

```json
"waited": {"last_ms": 42, "worst_ms": 108324, "behind": false, "behind_over_ms": 2000}
```

It is the one number that says a node has fallen behind while everything else about it
still looks right: it is online, its heartbeats arrive, its sources are ready, and what it
sends is old. Over two seconds the hub says so once, and says so again when it stops:

```
07:21:01 WARNING pico-cablato is falling behind: its last reading waited 108324 ms before it could be sent
07:21:01 INFO    pico-cablato has caught up: its last reading waited 31 ms
```

That pair is from the hub being stopped for seven hours on 2026-09-18 and started again:
three nodes, each one line each way, and nothing else about them had changed.

The events that do count are handed to Sentry, where `sensor_event`, `threshold` and
`sequence` rules can act on them; see [rules, second version](rules-v2.md). While a node
is offline, the rules that listen to it are paused or stop Sentry, as the rules' fault
policy says.

If the disk is full or the file is damaged, the hub keeps running its own cameras and
rules, reports the journal as unavailable, and stops admitting satellite events it
cannot keep. Nothing is acknowledged that was not written.

To look at the journal, copy it, or trim it:

    python scripts/satellite_admin.py journal
    python scripts/satellite_admin.py journal --snapshot /var/backups/satellites.sqlite3
    python scripts/satellite_admin.py journal --forget-events-older-than 30
    python scripts/satellite_admin.py journal --forget-receipts-older-than 30

Use `--snapshot` rather than copying the file: the journal runs in write-ahead mode, and
a plain copy can be missing the newest events. Forgetting events leaves the receipts in
place, so an old event still cannot be admitted twice; receipts younger than a day are
never forgotten. The hub also trims both once an hour by `journal_days` and `dedup_days`.

## Sensors

A node reads the sources listed in its `[[sources]]`. Each one has an `id`, a `kind` and
the options of that kind; anything else is refused when the file is read, with the name of
the option. Two sources may not share a pin, a probe, an I2C address and measurement, or a
pin that a bus already uses.

| Kind | What it reads | Options |
|---|---|---|
| `gpio` | A PIR or a contact on one pin. Sends `sensor.motion` (or `event_kind`) with `true` (active) or `false` (idle). | `line_numbering = "bcm"` (required), `line`, `active_high`, `bias` (`disabled`, `pull_up`, `pull_down`, `as_is`), `debounce_ms`, `settle_seconds`, `chip` |
| `onewire` | A DS18B20 probe, in °C. | `device` (`28-…`), `line` (the 1-Wire pin, default 4), `interval_seconds` |
| `bme280` | Temperature (°C), humidity (%) or pressure (hPa) — one source per quantity. | `measure`, `bus`, `address` (`0x76` or `0x77`), `interval_seconds` |
| `adc` | One channel of an ADS1115, for an LDR divider or another analogue part. | `channel`, `output` (`ratio` or `volts`), `reference_volts`, `bus`, `address`, `interval_seconds` |
| `board` | The board itself: its temperature in °C (`board.temperature`) and the share of the time its processor was working, as a percentage (`board.cpu`). | `measure` (`temperature` or `cpu`), `interval_seconds` |
| `dummy` | A simulated value, for testing the link. | `interval_seconds` |

Every source also takes `enabled = false`, which keeps it in the list without reading it.

**The board's own two are there without being asked for.** Every node reports
`board-temperature` and `board-cpu` every 30 seconds, whatever else is wired to it,
because neither needs a wire and a node that is quietly cooking or thrashing is worth
seeing before it starts missing readings. Naming either of them in `[[sources]]` — to read
it less often, or to turn it off with `enabled = false` — is left to say what it means:

    [[sources]]
    id = "board-cpu"
    kind = "board"
    measure = "cpu"
    interval_seconds = 300

The heartbeat carries the same two numbers every few seconds for the health display; the
sources are what puts them in the journal, where they can be looked back over.

What the board needs first, in `/boot/firmware/config.txt`, followed by a reboot:

    dtoverlay=w1-gpio            # DS18B20, on GPIO 4 unless gpiopin= says otherwise
    dtparam=i2c_arm=on           # BME280 and ADS1115, on /dev/i2c-1 (GPIO 2 and 3)

and the packages: `sudo apt install python3-libgpiod python3-paho-mqtt`. The service runs
in the `gpio` and `i2c` groups; `sentry-satellite doctor` says what is missing.

A few things worth knowing before wiring:

- **A PIR needs time to settle.** Set `settle_seconds` to what the module actually needs
  after power-up — some need a minute. Edges in that time are ignored, and the level at
  the end is sent as a baseline, which never starts a rule.
- **`debounce_ms`** is how long a change has to last. A contact switch usually needs 20 to
  50 ms; a PIR has its own hold time and needs little.
- **An LDR is not a lux meter.** It needs the ADS1115 because the board has no analogue
  input, and it reports a fraction of the supply or a voltage, which depends on the
  resistor next to it. A `threshold` rule on it has to be calibrated against the room.
- **A failed read is `unavailable`, never zero.** A probe that is unplugged, a bus that
  does not answer or a reading the part itself flags as bad is reported once as
  unavailable, and a threshold rule does not fire on it.
- **A DS18B20 reading exactly 85 °C** is its power-on value, not a temperature, and is
  refused.

The drivers and their choices are described in [sensor drivers](adr/satellite-sensors.md).

## Cameras

A board with a camera on its CSI port declares it as a source like any other, in the
`camera-sensor` profile:

```toml
[[sources]]
id = "camera-1"
kind = "csi"
width = 640          # 320x240, 640x480 or 1280x720
height = 480
fps = 10             # 1 to 15
bitrate_kbps = 1000  # 100 to 4000
keyframe_seconds = 2 # how soon the hub can join, or rejoin, a stream
```

A board has one camera port, so one `csi` source. It needs `rpicam-vid`
(`sudo apt install rpicam-apps-core`) and the service user in the `video` group;
`sentry-satellite doctor` names the sensor it finds, or says there is none.

The camera sends nothing on its own. When it is declared, the hub adds it to its own
cameras as `zero-entrance.camera-1`. A rule can watch it for objects, take photos and
videos from it ([rules](rules-v2.md#where-the-evidence-comes-from)), and the Video view
can show it. Only while something holds it — an armed rule, a page showing it, a
recording — does the hub ask the node for video; the node encodes in the GPU and sends
the H.264 as it comes, over TLS, to the hub's media port. The hub decodes it, runs the
shared detector on it when a rule asks, and keeps only the newest frame. A video from it
is silent unless the rule names a microphone.

On the hub, the media port is part of the satellite settings and uses the same
certificates as the broker:

```yaml
satellites:
  media:
    port: 8555                 # the satellites connect here, as well as to 8883
    stream_seconds: 60         # a stream nobody renews ends by itself on both sides
    renew_every_seconds: 20
    max_streams: 4
```

Nothing is needed per node: a stream is only accepted from the node it was opened for,
with the certificate that node was approved with, carrying a one-off token the hub sent it
a moment before. Revoking a node, or the node going offline, cuts a stream that is already
running. If the port cannot be opened — taken by something else, say — the hub says so on
the satellites status and carries on with events only.

The `satellites.media` block of `/api/satellites` shows the open streams; each camera's
own status says whether it is `starting`, `live`, `stale` (connected, nothing arriving) or
`offline`, the rate actually delivered, and why the last stream ended.

### Watching any camera

The Video view has a camera list once the hub has more than one camera. It shows each
camera's state, how old its newest picture is, and whether it is on this hub or on a
satellite. Each page that shows a camera holds its own preview session, so closing one
page never stops a camera another page, a rule or a recording is using.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/cameras` | — | `cameras`: `source_id`, `display_name`, `origin`, `zone`, `primary`, `state` (`idle`, `starting`, `live`, `stale`, `offline`), `frame_age_seconds`, `fps`, `error`, `previews` |
| POST | `/api/cameras/preview/start` | `{"source_id"}` | `session`, `source_id`, `expires_in_seconds`, `stream` |
| POST | `/api/cameras/preview/renew` | `{"session"}` | as start |
| POST | `/api/cameras/preview/stop` | `{"session"}` | `{"closed": true}`, or `false` if it had already ended |
| GET | `/api/cameras/preview/stream?session=…` | — | MJPEG, up to 10 frames per second and 960 pixels wide |
| GET | `/api/cameras/snapshot?source_id=…` | — | a JPEG of a picture no older than two seconds, or `409` |

A session ends 30 seconds after its last renewal, unless its stream is still open; the
page renews it every 10 seconds and stops it when it is closed. At most 16 sessions are
open at once. A stream ends after 10 seconds without a new picture, so a page notices
and asks again. The POSTs need the usual `X-Sentry-Mode-Control: 1` header.

The older `/api/video…` endpoints are still this hub's own camera, whatever camera a page
shows. A session on `legacy-primary` switches its preview on if it was off, and off again
when the last such session ends; a preview started with `/api/video/start` is left as it
was, and `/api/video/stop` still stops it for everybody. Why this is raw
H.264 over TLS rather than RTSP is in [the video profile](adr/satellite-video-profile.md),
with the measurements.

## Microphones

A board with a microphone declares it in the `audio-sensor` profile (or alongside a
camera, since an I²S microphone leaves the camera port free):

```toml
[[sources]]
id = "mic-1"
kind = "microphone"
device = "plughw:0,0"          # the ALSA capture device, as arecord --list-devices names it
interface = "i2s"              # or "usb"; an I²S microphone holds GPIO 18 to 21
activity = true                # report audio.activity; off by default
activity_threshold_dbfs = -35  # -90 to -1
activity_min_seconds = 0.3     # loud for this long before it says so
activity_hold_seconds = 3.0    # quiet (6 dB under the threshold) for this long before it says false
```

It needs `arecord` (`sudo apt install alsa-utils`) and the service user in the `audio`
group, which the unit sets; `sentry-satellite doctor` lists the capture cards it finds.
Two microphones may not capture from the same device. Capture is always 16 kHz mono.

The microphone sends no sound on its own. With `activity` on, it keeps a capture running
and reports `audio.activity` — `true` when the level has stayed above the threshold for
`activity_min_seconds`, `false` when it has been quiet for `activity_hold_seconds` — and
nothing about what it heard. With `activity` off it opens nothing at all until the hub
asks.

The hub adds it to the microphones it can hear as `zero-entrance.mic-1`. Only while
something holds it — a browser listening, a rule recording — does the hub ask for sound;
the node sends it, as numbered blocks of raw PCM, to the same media port and with the same
checks as video. The hub puts the blocks back in order, fills a short gap with silence,
and gives each reader its own two-second queue, so a slow browser loses its own oldest
sound and nobody else's. Two browsers can listen to one satellite microphone at once.
Why this is not a WebSocket, and the block layout, are in
[the audio profile](adr/satellite-audio-profile.md).

A rule can record from it (an `audio` step, or a video's sound) and be set off by it
([sound on a satellite](rules-v2.md#sound-on-a-satellite)). The Video view's **Enable
audio** has a list of microphones once there is more than one.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/microphones` | `microphones`: `source_id`, `display_name`, `remote`, `zone`, `state`, `listeners`; a satellite's also has `source_state`, `error`, `level_dbfs` and `blocks` (`blocks`, `duplicates`, `gaps`, `node_gaps`, `lost_samples`, `filled_samples`) |
| GET | `/api/audio/monitor?source_id=…` | raw 16-bit mono PCM at 16 kHz from that microphone; `404` for one the hub does not know, `409` when two browsers already listen |

The node's own health reports, for each microphone, whether its capture runs, its last
error and its level.

A Pico W or Pico 2 W takes a microphone too, and reports the same `audio.activity` with the
same thresholds and the same hysteresis — the firmware carries its own copy of those rules
for the same reason it carries the presence ones. It can also be listened to. The hub shows
it in `/api/microphones` as `pico-ingresso.hall-noise` like any other satellite microphone,
and a browser that opens it makes the board dial the media gateway on a second TLS
connection and send the same blocks of sound a Pi sends.

Nothing is kept on the board. A block is measured, handed to that connection if somebody is
listening, and then overwritten — there is no recording on a microcontroller and no way to
ask one for what it heard a minute ago. When nobody is listening there is no second
connection at all, and the only thing the microphone produces is `audio.activity`.

```json
{"id": "hall-noise", "kind": "microphone", "pin": 6, "clock_pin": 7,
 "channel": "left", "activity_threshold_dbfs": -35.0,
 "activity_min_seconds": 0.3, "activity_hold_seconds": 5.0}
```

- It is pins rather than a sound card: `pin` is the microphone's data line, `clock_pin` is
  the bit clock, and the word select is the pin above the clock — the hardware drives those
  two from one register, so it is not a choice. `device`, `interface` and `activity` are
  not among its options, and a source that names one is refused on the page.
- `channel` is `left` or `right`: which half of the frame the microphone is wired to drive,
  which on most parts is a pin tied high or low. Capture is 16 kHz, as it is on a Pi.
- `activity` is not an option because it is the only mode. A microphone on one of these
  boards is an activity detector and nothing else.
- One microphone to a board. There is one state machine clocking I²S and one pair of
  buffers behind it, so a second source is refused before it is sent rather than quietly
  started on the first one's clock.
- While the hub is listening, the board holds four blocks — four hundred milliseconds. If
  the connection falls behind, the oldest block that has not started going out is dropped
  and the hole is marked, so the hub fills it with silence and the sound after it stays
  where it happened. The half-sent block is never the one thrown away.

## Presence

A `sensor-presence` node can watch for one known device over Bluetooth LE: a beacon, or a
tag whose address does not change. It is a device, not a person, and nothing it says
disarms anything.

```toml
[[sources]]
id = "presence"
kind = "ble"
address = "AA:BB:CC:DD:EE:FF"  # or ibeacon_uuid, for a beacon that changes address
rssi_min = -95                 # weaker than this is another room, not this one
enter_sightings = 3            # sightings, spread out, before it counts as here
enter_window_seconds = 10.0
absent_after_seconds = 120.0   # of real scanning without a sighting
```

| Option | What it does |
|---|---|
| `adapter` | Which adapter to scan with, `hci0` by default. Several sources may share one. |
| `address` | The device to watch, in capitals. Exactly one of this and `ibeacon_uuid`. |
| `ibeacon_uuid`, `ibeacon_major`, `ibeacon_minor` | An iBeacon instead of an address; the major and minor are optional and only narrow it. |
| `rssi_min` | Sightings weaker than this are ignored, −100 by default. |
| `enter_sightings`, `enter_window_seconds` | How many sightings, in how long, make it present. Several in the same second count once. |
| `absent_after_seconds` | How long the scanner has to run, seeing nothing, before it says absent. |
| `event_kind` | `presence.state` unless you change it. |

It needs BlueZ running (`bluetoothd`, already there on Raspberry Pi OS), the service user
in the `bluetooth` group, and the adapter not blocked — `rfkill list bluetooth`, and
`sudo rfkill unblock bluetooth` if it is. `sentry-satellite doctor` says which of these is
missing. Scanning is not qualified on a board that also streams video or sound, so the
`camera-sensor` and `audio-sensor` profiles refuse it.

The node sends `presence.state` with `present` or `absent`, and nothing else: no address,
no signal strength, and no word about any other device in the air. What it saw stays on
the node. Two rules matter and both are about not guessing:

- **A sighting is a sighting.** BlueZ keeps devices it has seen before, and reading one of
  those again is not news; only an advertisement received while this scan is running
  counts. Sightings have to be spread out, so one burst of packets is one sighting.
- **Silence is not absence.** Absence needs `absent_after_seconds` of scanning that really
  ran. If the adapter is blocked, the scan stops, `bluetoothd` restarts or the node is not
  connected to the hub, the source goes to `unknown` and stays there until scanning works
  again. The node's health says whether the scanner is up, what stopped it and when it
  last saw the device.

A rule is set off by it as [a device arriving or leaving](rules-v2.md#a-device-arriving-or-leaving),
which can name several nodes watching the same device. Why BlueZ over D-Bus, and what a
scan costs on a Zero W, are in [presence](adr/satellite-presence.md).

A Pico W or Pico 2 W watches the same way, with the same words and the same meanings: the
firmware carries its own copy of these rules so that the hub cannot tell which kind of node
an arrival came from. The differences are what the board does not have rather than what it
does:

- `adapter` is not one of its options. There is one radio, and nothing to choose between.
- The radio is on the same chip as the Wi-Fi. A board with no wireless chip — a plain Pico
  or Pico 2 — refuses a `ble` source at configuration time, and its firmware image has no
  Bluetooth in it at all.
- `enter_sightings` is at most 20, `enter_window_seconds` between 1 and 120, and
  `absent_after_seconds` between 10 and 3600. A window too short to hold the sightings it
  asks for — they are a second apart at best — is refused rather than left to wait for an
  arrival that can never be counted.
- The node never advertises, never pairs and never bonds. It listens passively, and keeps
  nothing about any device across a reset.

Both halves of the same node are checked before anything is sent: a source that names
neither an address nor a beacon, or both, or that puts an `ibeacon_major` on an address, is
refused on the page rather than after a round trip.

## Installing a node, updating it, and going back

A node runs a release: a tar of the agent's source tree with a manifest beside it that
names every file by its SHA-256. It is built from a checkout, not on the board, and the
same tree always produces the same bytes, so two boards holding the same release id are
running the same code.

    python -m sentry_satellite.cli package --into dist     # on your machine
    scp dist/sentry-satellite-0.1.0.tar.gz pi@zero-entrance:
    sudo ./install.sh --release sentry-satellite-0.1.0.tar.gz    # on the board

The install unpacks the release beside the one before it, checks every file against the
manifest, and only then moves `/opt/sentry-satellite/current` to it — one symlink, so a
board that loses power at the wrong moment is running one release or the other and never
half of each. Running it again with the same release changes nothing. It writes
`/etc/sentry-satellite/node.toml` from the example only if there is none, and never
touches an identity, a certificate or a service that is not its own.

Then the board is told what it is, once, by a person with root — the agent runs as its own
user and never names itself:

    sudo -u root env PYTHONPATH=/opt/sentry-satellite/current/src \
        python3 -B -m sentry_satellite.cli identity --create zero-entrance

The name has to be the one in the node's certificate, and the hub has to have approved it
already. After that, `/etc/sentry-satellite/node.toml` says where the hub is and which
certificates to use, and `systemctl enable --now sentry-satellite` starts it.

    cd /opt/sentry-satellite/current/scripts    # they travel with the release
    sudo ./rollback.sh --list
    sudo ./rollback.sh --to 0.1.0
    sudo ./backup.sh --into /var/backups

A backup holds what makes a node that node — its configuration, identity, certificates and
the sources the hub last sent it — and not the release, which can be installed again from
its own file. It contains a private key, so it is written `0600` and belongs somewhere
that stays that way. `rollback.sh --to <version> --data <backup>` puts both back together,
because a certificate and the file that says who the node is belong to the same moment or
to neither.

`sentry-satellite verify` says whether what is installed is still what was packaged, file
by file, and names anything that has appeared inside a release that was not part of it.

A microcontroller has none of this — no filesystem, no release directory and no symlink to
move. It has one image, which is copied onto it whole, and a manifest that is written
beside that image rather than installed with it: the commit, the SDK, the hash of the
`.uf2`, what the hub will let a node of that kind be asked for, and what that firmware
still does not do. `make pico-release PICO_BOARD=pico2_w` writes it, and
[the firmware's own README](../firmware/pico/README.md) has the whole walk-through — the
four steps from a board out of its bag to a node in the journal, and the five ways of
getting one back.

## Changing a node's sources from the hub

The **Satellites** page, in the menu once satellites are on, lists every node: whether it
is fresh, stale or offline, its agent version, its sources with their state and latest
reading, and what went wrong recently.

An approved, online node can be given a new list of sources from its card. Open *Change
sources*, edit the list — it is the node's `[[sources]]` written as JSON — and send it.
Only the kinds in the tables above can be sent, and only those the node's own platform has
a driver for; a USB camera is refused with a note on when it arrives. The network, the certificates and the hub address
cannot be changed this way, on purpose.

Each change has a revision number, higher than the last. The node checks the whole list
before touching anything, stops the old drivers, starts the new ones and watches them for
two seconds. If a driver cannot start — a pin held by another program, say — the node
goes back to the list it had, and the card says why. What was applied is kept in
`/var/lib/sentry-satellite/sources.json` and used at the next start in place of the
sources in `/etc/sentry-satellite/node.toml`, which is never rewritten. To go back to the
installed file, stop the service, delete that file and start it again.

A node takes one change at a time. A second one sent while the first is starting is
refused and can simply be sent again; the hub waits up to 30 seconds for an answer before
it lets you try.

The same is available to scripts, with the control header:

    curl -s localhost:8083/api/satellites
    curl -s -X POST localhost:8083/api/satellites/configure \
      -H 'X-Sentry-Mode-Control: 1' -H 'Content-Type: application/json' \
      -d '{"node_id": "zero-entrance", "sources": [{"id": "pir-1", "kind": "gpio", "line_numbering": "bcm", "line": 17}]}'

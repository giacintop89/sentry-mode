# Pico satellite firmware

A satellite that is a microcontroller rather than a Linux board. This directory holds the
firmware for it, and most of what it holds is the part of that firmware which can be built
and run without one: the protocol — JSON, the event envelope, the command grammar, the
state, health and answers it writes, the topics it writes them to, and the lease — and the
core it will sit on — the two configuration slots, the identity, the timebase, the queue
and the order a connection has to come up in. All of it compiled for the host and tested
against the same contracts and fixtures the hub and the Pi Zero agent are tested against.

It has now also run on a board. A Raspberry Pi Pico 2 W (RP2350) was flashed on
2026-09-17 with the device build below, and the events it published from its own die
temperature were accepted by the hub's models and by the published schema — the same
serializer, the same bytes, on a chip where `long` is 32 bits and unaligned access is not
free. The same board has since joined a network, taken its time from it, proved who it
was to this system's own Mosquitto over mutual TLS, announced itself, and been granted,
renewed, revoked and stopped by a hub speaking through the broker. It keeps its identity,
its credentials and the last configuration it was given in its own flash, comes back from
a reset as itself with nobody at the cable, and resets itself when its loop stops turning.
What is still not on the board is a sensor with wires on it, I²C, and the media this plan
puts after the sensors. The hardware gates in the
[implementation plan](../../docs/sentry-mode-pico-implementation-plan.md) that this has
executed are named where they were executed, below; the rest stay marked **not
executed**.

## Building and running the tests

`cmake` 3.20 or newer and a C++17 compiler. Nothing else — no SDK, no network, no board.

```sh
cmake -S firmware/pico -B build/pico-host -G Ninja
cmake --build build/pico-host
ctest --test-dir build/pico-host --output-on-failure
```

Seventeen suites: `json`, `command`, `control`, `event`, `lease`, `topics`, `mqtt`,
`client`, `session`, `store`, `identity`, `credentials`, `timebase`, `spool`, `input`,
`sensors`, `pins`. The `command` suite
reads the fixtures in
`contracts/satellite/v1/control/fixtures/`, the same files `tests/unit/test_satellite_control.py`
and `satellite/tests/unit/test_control_contracts.py` read, so a fixture the hub accepts and
this firmware refuses is a failure here rather than a surprise on a board.

`make pico` runs all three steps and the cross-check below in one go.

Then the cross-check, which is the one that matters most:

```sh
.venv/bin/python firmware/pico/tools/check_against_contracts.py --build-dir build/pico-host
```

It runs the firmware's own serializers (`sentry_emit`) and hands everything they write to
the hub's Pydantic models and to the satellite's schema checker: the events, the state,
health and answers, and the topics — which are also read back by the hub's own topic parser
and compared with what the Linux agent's `topic()` would have written. A serializer validated
against the schema it was written from proves very little; one validated by two
implementations that have never seen it proves something. It has already earned its place:
it caught this firmware calling a reading `stale` when the hub's vocabulary says
`degraded`, which every test in this directory was happy to accept.

The same idea applies to the packets themselves:

```sh
.venv/bin/python firmware/pico/tools/mqtt_check.py --build-dir build/pico-host
```

`mqtt.cpp` was written from the MQTT 3.1.1 specification; so was the decoder in that script,
and neither has read the other. It decodes the CONNECT, the SUBSCRIBE and the four
PUBLISHes the firmware would send and then asks the hub about them: the will has to be a
goodbye the hub would accept, retained and at QoS 1 and small enough for lwIP to carry; the
topics have to be ones the hub's parser reads back as this node's; every payload has to
pass the models and the published schema. A packet a broker would drop — SUBSCRIBE with the
wrong reserved bits, a PUBLISH at QoS 1 with no packet id — is caught here rather than by a
broker closing the connection for a reason nobody can see from the board.

The same script then reads a whole connection rather than a packet at a time. `client.cpp`
is driven through one by `tools/client_run.cpp`, against a broker made of bytes written
from the specification, and the decoder is asked whether what came out is a node behaving:
the CONNECT, the SUBSCRIBE, nothing announced until the SUBACK and nothing published until
the hub has been told this node is here, an event that went unacknowledged coming back
with DUP under the same packet id rather than as a second event, the acknowledgement a
command is owed, and the ping that keeps a quiet link open. A packet can be perfect and a
node can still be wrong about when to send it.

And to the flash framing:

```sh
.venv/bin/python firmware/pico/tools/pack_provisioning.py --build-dir build/pico-host --verify
```

That writes configuration slots from the header layout rather than from the C++, and asks
the firmware (`sentry_unpack`) what it read back — including from a record whose last three
bytes never made it, which must leave the previous configuration in charge.

## Building for a board

The SDK is not vendored. Clone it once, wherever you keep such things:

```sh
git clone --depth 1 --branch 2.2.0 https://github.com/raspberrypi/pico-sdk ~/.local/share/pico-sdk
cd ~/.local/share/pico-sdk && git submodule update --init --depth 1 \
    lib/tinyusb lib/cyw43-driver lib/lwip lib/mbedtls
```

Then, with `gcc-arm-none-eabi` installed:

```sh
make pico-device                      # or: PICO_BOARD=pico_w make pico-device
```

That writes `build/pico2_w/sentry_firmware.uf2`, and then checks the image for anything
that should not be in it. Four boards are built by the workflow and were built by hand on
2026-09-17:

| `PICO_BOARD` | Chip | Radio | UF2 |
|---|---|---|---|
| `pico2_w` | RP2350 | yes | 1.27 MB |
| `pico_w` | RP2040 | yes | 1.32 MB |
| `pico2` | RP2350 | no | 357 kB |
| `pico` | RP2040 | no | 377 kB |

The wired ones are a quarter of the size because they compile a different network module —
`net_wired.cpp`, which answers "no radio" and nothing else — and with it no lwIP, no
mbedTLS and no TLS stack at all. What they compile instead is `bridge.cpp`: the USB port,
carrying frames to the machine the board is plugged into. Everything else is the same
firmware, and the same file writes the messages either way. `pico2_w` and `pico2` have both
been run on hardware — the second one on the same board, which is a Pico 2 W with its radio
never brought up.

Nothing is compiled into any of them that belongs to a particular board.
`tools/image_check.py` is what says so: it refuses an image carrying a PEM block with a
body in it, and — when it is pointed at one — anything out of a provisioning record. A
board becomes this node by being told over the cable, and it writes that down itself.

Hold BOOTSEL while plugging the board in —
or, if it is running MicroPython, `import machine; machine.bootloader()` — and copy the UF2
onto the `RP2350` volume that appears. The board reboots into the firmware.

`src/device/main.cpp` is one small program and not a satellite. It brings up USB serial and
the wireless chip, names itself from the board's own serial number, makes its boot id from
`pico_rand`, reads the die temperature through `sensors.cpp` and publishes it with the same
`write_event` the host tests exercise — to the serial line, to the queue in `spool.cpp`,
and, once it has been told who it is and given a certificate, to the broker. It has no clock of its own, so it publishes nothing
until something tells it the time — a board that stamped readings from the moment it booted
would be writing timestamps nobody measured.

What it listens for on the serial line:

| Line | What it does |
|---|---|
| `provision <json>` | The identity and the network, as `read_provisioning` reads them, written to flash and read back at the next boot. |
| `join` | Joins that network and starts asking `pool.ntp.org` what time it is. |
| `credentials <json>` | The authority, the certificate and the key, as PEM. Nothing prints any of it back. |
| `connect` | Opens the one connection this node makes, and keeps making it again if it drops. |
| `disconnect` | Says goodbye on it, then closes it and stops trying. |
| `command <json>` | One command, down the same path a command from the broker takes: the lease, the answer, and the baseline if it is a new grant. For a bench with no hub at the other end. |
| `status` | Address, signal, clock, credentials, socket, free heap, what the client thinks, what is queued, the lease, and every source in the running plan. |
| `time <unix_ms>` | The time, for a board with no network to ask. |
| `sample` | Every source read now, rather than at the next interval. |
| `drive <pin> <0|1>` | Drives a pin the plan holds, as a sensor on it would. A board with nothing wired to it can still be made to have something happen. |
| `keeping` | What is in the flash: the five things, by name and by how many times each has been written. Never what is in them. |
| `forget [what]` | Erases one of them, or all of them, and clears it out of memory too. The recovery verb. |
| `tear <what>` | Writes half a record into the slot that is not in use, on purpose, so the next boot can be watched refusing it. |
| `hang` | Stops feeding the watchdog. The board resets itself a few seconds later, which is the only way to see that the watchdog works. |

The record holds a passphrase and points at a private key, so it is handed over rather than
committed:

```sh
firmware/pico/tools/provision_board.py --record .local/pico-provisioning.json --join --connect
```

`.local/` is ignored by git. Nothing prints the passphrase or the key back — not the tool,
not the board — and `identity.cpp` refuses a record with a key for a network it was never
given, because joining the wrong network is worse than refusing to join one. The three PEM
files are the ones `scripts/satellite_admin.py` issues: the authority this node checks the
broker against, and the certificate and key it answers with. `credentials.cpp` refuses a
private key offered as a certificate, which is the mistake that happens at a serial port,
and the board will not open a connection without all three — there is no plaintext path to
the same broker to fall back to.

The check that makes it worth having:

```sh
.venv/bin/python firmware/pico/tools/board_check.py --port /dev/ttyACM0 --events 3
```

It gives the board the time, reads what it publishes, and hands each event to the hub's
Pydantic models and the satellite's schema checker — the cross-check of the section above,
with the firmware running on the chip instead of on a computer. The first run, on a Pico
2 W:

```
ok  board-temperature seq=0 32.66 °C valid clock=synced at 2026-09-17 11:56:24.789000+00:00
ok  board-temperature seq=1 33.13 °C valid clock=synced at 2026-09-17 11:56:26.814000+00:00

3 events from pico-cde2f882a116bf77, judged by the hub's models and the schema
```

The binary is 488 kB of text and 112 kB of static RAM, most of it the wireless firmware,
TinyUSB and mbedTLS; `arm-none-eabi-size build/pico2_w/sentry_firmware.elf` says so on any
change. The TLS half of that is the reason `src/device/sentry_mbedtls_config.h` exists:
one curve, one key exchange, one cipher, a 4 kB record in and 2 kB out. What is not
compiled in cannot be negotiated down to.

The joining and the time have been watched to work on the same board, on 2026-09-17:

```
# provisioned as pico-ingresso, broker 192.168.11.240:8883, network GL-SFT1200-fda
# joining GL-SFT1200-fda
# joined GL-SFT1200-fda as 192.168.11.156, -44 dBm
# the network says it is 2026-09-17T13:04:19.812Z (answer 1)
# node=pico-ingresso provisioned=yes link=up address=192.168.11.156 signal=-49 clock=synced answers=1
```

Association, DHCP, the DNS lookup and the SNTP answer all happen inside `net.cpp`; the
answer is handed to `Timebase` on the main loop rather than in the callback, so nothing
that publishes is running on lwIP's stack. Every event published after it carries
`clock_status: synced`, and the timestamps it writes agree with this machine's clock to
within 10 ms — which is the whole point of asking the network rather than a person.

And then the broker itself, on the same board and the same afternoon:

```
# credentials: authority yes, certificate yes, key yes
# connecting to 192.168.11.240:8883
# the broker accepted this node
# listening on sentry/v1/nodes/pico-ingresso/commands
# online, as pico-ingresso
# node=pico-ingresso … clock=synced answers=1
# credentials=yes socket=open mqtt=online queued=0 coalesced=1 dropped=0
```

That is a real mutual-TLS handshake against a real Mosquitto with `require_certificate
true` and `use_identity_as_username true`, an ACL generated from the hub's own registry,
and a node registered and approved with `satellite_admin.py` as a `pico-2w-sensor`. What
came out the other side, watched with `mosquitto_sub` as the hub:

```
sentry/v1/nodes/pico-ingresso/state  {"schema_version":1,…,"online":true,…}
sentry/v1/nodes/pico-ingresso/events {"schema_version":1,"event":{…"board.temperature"…}}
```

The certificate's dates are checked, which needs a clock: mbedTLS is given the time the
network last said it was, and nothing at all before an answer has arrived, so a board that
has not been told the time does not connect rather than accepting an expired certificate
it has no way to judge.

The other half of that run is the one worth reading. Handed an authority that had signed
nothing in this system — a throwaway CA made for the purpose — the board connected anyway,
announced itself and published. lwIP's default is `MBEDTLS_SSL_VERIFY_OPTIONAL`: the
broker's certificate is verified and the answer is then thrown away. `lwipopts.h` now sets
`ALTCP_MBEDTLS_AUTHMODE` to `MBEDTLS_SSL_VERIFY_REQUIRED`, and the same test says:

```
--- somebody else's authority
# connecting to 192.168.11.240:8883
# the broker closed the connection (trying again in 1025ms)
# the broker closed the connection (trying again in 2152ms)
# the broker closed the connection (trying again in 4041ms)
…                                   (8316, 16416, 32228, and then a minute)
--- the authority this system runs
# the broker accepted this node
# online, as pico-ingresso
```

A board that cannot get in waits longer each time, up to a minute, with a little of its own
randomness in it so that a houseful of them coming back after a power cut does not arrive
in step. That is `T06` for a wrong authority and `T23` for the backoff; an expired
certificate, one issued for another name and a revoked one are still **not executed**.

### Answering the hub

A node that only publishes is a node nobody can stop. The board now reads the commands it
subscribed to, decides what they mean with `lease.cpp`, and answers every one of them on
its `acks` topic. Nothing is published until a hub has granted it: the readings taken
before that wait in the queue, coalescing as they go, and `queued=16 coalesced=19` is a
board that has been taking its own temperature for a while and telling nobody.

The whole of it, driven from the hub's side of the broker with `mosquitto_pub` and watched
from both ends on 2026-09-17:

```
# grant=none epoch=0 expires_in=0s owed=0 answered=0 unanswered=0
--- a grant, for a minute
# grant 7675bd49-…: applied
# grant=8ef02d9c-… epoch=41 expires_in=54s owed=0 answered=1        queued=0
--- the same grant again, the ack having gone missing
# grant 7675bd49-…: applied, already handled
# grant=8ef02d9c-… epoch=41 expires_in=49s owed=0 answered=1
--- a renewal that is not newer than the grant it renews
# renew edfbc700-…: failed, a renewal that is not newer than the grant it renews
--- a renewal that is
# renew a396f1ef-…: applied
# grant=8ef02d9c-… epoch=41 expires_in=116s
--- a configuration this firmware has no sources for
# configure e3d18c40-…: failed, this firmware carries one source of its own and cannot yet be configured
--- something that is not a command
# a command arrived that this node will not act on: malformed
--- a grant addressed to another node
# a command arrived that this node will not act on: not_for_this_node
--- taking it back
# revoke 6eb5f240-…: applied
# grant=none epoch=41 expires_in=0s answered=5                      queued=4
--- stop
# stop 7af2072c-…: applied, stopping
# gone, as asked
```

The second line of each pair is what `status` said afterwards. The duplicate is the point
of the third: the same `command_id` is answered again — `applied, already handled`, the
answer it was given the first time — and the expiry goes on counting down from 54 to 49
seconds rather than starting over. That is `T11`. A renewal that is not newer than the
grant it renews is refused for the same reason.

On the broker's side, in order: the retained `state` with `online: true`, seven acks for
six commands, the readings that had been waiting, and among them one with

```json
"delivery":{…,"hub_epoch":41,"grant_id":"8ef02d9c-…","initial_state":true}
```

which is the baseline — where the source stands, taken the moment the grant arrived so
that a hub which has just restarted is not told about a change it has no `before` for. It
is queued like a thing that happened once, so the periodic reading two seconds later
cannot quietly take its place. After the revocation, nothing: the queue fills again and
the broker is told nothing, which is `T10`. Every event between the grant and the
revocation carries the epoch and the grant id it was published under. That is `T09` and
`T12` for what a node can show of them; the hub's own half of `T12` — an old will not
ending a new session — is the hub's to prove.

Beside all of that, every fifteen seconds and whether or not anything has been granted,
the node says how it is:

```json
{"schema_version":1,"node_id":"pico-ingresso","boot_id":"22328041-…",
 "agent_uptime_seconds":65.1,"clock_status":"synced",
 "queue":{"events":16,"bytes":5504,"published":0,"refused":0,
          "drops":{"count":8,"bytes":0,"age":0,"total":8},"granted":false},
 "sources":{"board-temperature":{"readings":24,"driver":"running"}},
 "board":{"uptime_seconds":65.1,"temperature_c":36.9}}
```

That is a board nobody has granted anything: sixteen readings waiting, eight already lost
to make room for newer ones from the same source, and nothing published. A reading
replaced by a newer one is counted as a drop rather than left out, because the contract has
no separate word for it and a queue that showed no losses while it quietly lost eight would
be worse than one that reports them under the nearest true name. What the board cannot
measure it leaves out: there is no free-heap figure, because nothing here allocates.
Both of the messages above were handed to the hub's Pydantic model and to the published
schema, which took them.

### The hub, and the other node

None of the above involved the hub itself: `mosquitto_pub` was standing in for it. So the
last thing was to stop standing in. With Sentry Mode running on this machine, its satellite
subsystem connected to the same broker, and a Raspberry Pi Zero W and a simulated Linux
node already on it speaking MQTT 5, the board was plugged in and told to connect:

```
2026-09-17 15:49:35 INFO satellites.service zero-simulated is online in epoch 15 with 1 sources
2026-09-17 15:49:35 INFO satellites.service zero-w is online in epoch 16 with 2 sources
2026-09-17 15:49:35 INFO satellites.service pico-ingresso is online in epoch 17 with 0 sources
```

and the board, a second later, on its serial line:

```
# online, as pico-ingresso
# grant 08fbfbe7-…: applied
# grant=a661a265-… epoch=18 expires_in=272s owed=0 answered=1 unanswered=0
```

That is a hub granting a microcontroller it has never met, and the microcontroller taking
it. The hub's own journal, a few minutes later, has 42 events from `pico-ingresso` beside
3 379 from the simulated node and 116 from the Pi Zero W — one broker, one ACL, MQTT 3.1.1
and MQTT 5 side by side, which is `T13`. Of those 42, the hub classified one as `initial`
and the rest as `live`: the baseline arrived as a baseline and nothing else did.

The `0 sources` in that third line was the board saying nothing about what it has. It now
declares its one source in the same retained state, and the hub's satellites page shows it:

```json
{"source_id":"pico-ingresso.board-temperature","name":"board-temperature","kind":"board",
 "role":"sensor","state":"ready","declared":true,"enabled":true,"supported":true,
 "options":{"measure":"temperature","interval_seconds":2.0},"driver":"running"}
```

`supported` is the hub checking that a `board` driver is something a `pico-2w-sensor` may
have, from its own platform catalogue, rather than taking the node's word for it.

One thing in that run was not the firmware's doing and is worth writing down anyway: the
hub had been running since before the board was registered, and an older build of it could
not read the registry file that `satellite_admin.py` had rewritten, so it went on serving
the two nodes it already knew and never saw the third. Nothing in any log said so — the
reload is one `stat` and a refusal to reload is silence. A hub that has been up for longer
than the registry it is reading is a thing to check first.

Two of those lines were not there the first time. `stop` used to be the node closing the
socket, and the broker did what a broker does when a client vanishes: it published the
will, so the hub saw the node go offline twice, once because it was told to stop and once
as though it had fallen off the network. A goodbye is now said in full — what is owed,
then the retained `online: false`, then an MQTT `DISCONNECT`, which is what tells a broker
to keep the will to itself. The log above ends with one offline state, not two.

### Being told what to run

Until now the board ran what it was built with. It now runs what it is told, and the same
`Plan` decides both: the default at boot — its own temperature, every thirty seconds — is
built as a configuration and taken through `Plan::take` like any other, so there is no
second way into the plan and nothing to keep in step.

A configuration is judged whole on a copy before anything is touched, and refused whole
with a reason naming the source it failed on: a driver this firmware does not have, an
option belonging to somebody else's driver, a pin that is the radio's, a pin two sources
both asked for, a debounce nobody could wait for. A node that started half of one would be
a node running whatever survived, which is a thing neither the hub nor the person who wrote
the file could predict.

From the hub's own page, through its own API, with a PIR on GPIO 15 beside the temperature:

```sh
curl -H 'X-Sentry-Mode-Control: 1' -H 'Content-Type: application/json' \
  http://127.0.0.1:8083/api/satellites/configure -d '{"node_id":"pico-ingresso","sources":[
   {"id":"board-temperature","kind":"board","measure":"temperature","interval_seconds":20},
   {"id":"pir-1","kind":"gpio","pin":15,"bias":"pull_down","settle_seconds":2,
    "debounce_ms":50,"event_kind":"sensor.motion"}]}'
```

```
{"message": "Configuration 2 sent to pico-ingresso.", …}
# configure 70c467ff-…: applied
# plan=2 revision=2
#   board-temperature board measure=temperature every=20s readings=1
#   pir-1 gpio pin=15 level=high state=on readings=1
```

and the hub, reading back the retained state the board publishes again after adopting it:

```json
{"name":"board-temperature","kind":"board","options":{"measure":"temperature","interval_seconds":20.0},"state":"ready"}
{"name":"pir-1","kind":"gpio","options":{"pin":15,"active_high":true,"bias":"pull_down","debounce_ms":50,"settle_seconds":2.0,"event_kind":"sensor.motion"},"state":"ready"}
```

That is what the board is running, not what it was asked to run. The two are the same here,
and the whole point of publishing the first is that they need not be.

Then a wire. There is nothing wired to this board yet, so `drive 15 1` makes the pin an
output and drives it, and the input half reads the same pad back the way it would read a
sensor. The hub's journal, from a pin driven high, low, high:

```
pir-1 sensor.motion true  quality=unknown classification=initial eligible=0
pir-1 sensor.motion false quality=valid   classification=live    eligible=1
pir-1 sensor.motion true  quality=valid   classification=live    eligible=1
```

The first one is the baseline the board takes the moment a configuration is adopted, and it
says `unknown` because the PIR was given two seconds to settle and had not: where the source
stands, said out loud, with an admission that it is not yet worth acting on. The hub agrees
with both halves of that — it recorded it and marked it ineligible.

The third driver is the converter the chip already has. An `adc` source is a pin between
GPIO 26 and 28 read every so often, as a fraction of full scale or as volts, and it is
labelled for what it is: `ratio` is a relative figure and `volts` are volts, and neither is
lux — nothing here has been calibrated against a light meter. A pin outside that range is
refused as what it is, a pin with no converter behind it, rather than as a pin that does
not exist; and a configuration written for the other satellite, which reads its analogue
values through an ADS1115 on an I²C bus, is refused by the name of the option that gives it
away:

```
light-1: the converter is on GPIO 26 to 28, not on GPIO 15
light-1: an adc source has no chip
```

The first of those came back to the hub as the `detail` of a `failed` ack while the node
went on running the three sources it already had, which is the whole-or-nothing rule seen
from the other end.

There is a limit on the other side of that rule which the hub had not been checking. A
source here holds eight settings beside its `id` and its `kind` — `kMaxOptions` in
`command.h` — and everything written next to those two is one of them, `enabled` included.
A ninth is not a setting the board ignores: the parser gives up on the command it is inside,
so one crowded source takes the whole configuration down with it. A `ble` source is the one
that can reach nine, having that many to choose from. The hub now says so before sending,
measured against the running hub:

```
400 {"error": "pir-1: 9 settings beside its name and kind; a pico-2-wired takes 8, and the
     one too many is refused along with the rest of the configuration; …"}
```

The fourth is a bus rather than a pin. A `onewire` source is a DS18B20 on a GPIO, named
the way the Linux agent names one — `28-0123456789ab`, which is the kernel's spelling of
its ROM code — or left unnamed, which means the one probe on the bus. The name is worth
having: the bus prints the serial in the opposite order from the one it wants it in, so a
MATCH ROM built from the printed order addresses nobody, and the reversal is done once, in
`plan.cpp`, where a host test can watch it.

A conversion takes three quarters of a second, which is longer than this loop is willing to
stand still for: the probe is asked, and the answer is collected on a later turn of the
same loop that is keeping the connection alive. The bus work itself is in
`src/device/onewire.cpp` and is the one piece of this firmware that cannot be tested off a
board — the timing is microseconds, and interrupts are off for the slots that are measured
in them, because lwIP servicing the radio in the middle of one would turn a one into a
zero.

With nothing wired to GPIO 2, which is what this board has:

```
#   thermometer-1 onewire pin=2 device=the one on the bus every=10s readings=3
```
```
thermometer-1 climate.temperature null °C unavailable initial
thermometer-1 climate.temperature null °C unavailable live
```

Through all of that the connection, the die temperature and the converter carried on: a bus
with nothing on it is a source that has nothing to say, not a node that has stopped. That is
the shape of what `PICO-04` asks for when it says an I²C fault must not take MQTT and GPIO
with it — there is no I²C here, and this is the same failure on the bus there is.

Nothing answered the reset pulse, and the node says so every interval rather than saying
nothing: a probe that has fallen off its wire and a probe nobody asked about look identical
from the hub otherwise. What has **not** been seen is a probe that answers — there is no
DS18B20 on this desk, so the timing has only ever been watched failing to find one.

One thing that run found was not the firmware's. The hub acknowledges the messages it
receives by hand, and only the events path was acknowledging them: state, health and acks
were read, acted on, and left unsettled. The broker holds an unacknowledged QoS 1 message
in its window for that connection, and when the window fills it stops delivering at QoS 1
altogether. The Linux agent publishes its heartbeat at QoS 0, so it went on looking alive;
the Pico publishes everything at QoS 1, so it went silent from the hub's side while still
publishing every fifteen seconds, with nothing in either log to say so. The fix is in
`satellites/service.py` — everything that is not an event is settled as soon as it is
handled, because nothing about it is written down and nothing would be gained by having it
sent again — and `test_satellite_service.py` now holds a test that would have caught it.

### The biggest thing on a small stack

Both chips put the stack at the top of memory and the heap under it: `__StackTop` is the
end of the scratch banks and `__StackLimit` is eight kilobytes below it, and what is below
*that* is the heap growing the other way. Eight kilobytes is the whole budget for every
frame this program has at once.

A command did not fit in it. The wire grammar took 32 sources of 8 options because that is
what the contract lets a hub send *any* node, and the structure that holds one was 56,808
bytes on the host build. `parse_command` was handed one and cleared it with `out =
Command{}` — which builds the empty one somewhere before copying it — and three functions
kept one as a local. Compiled with `-fstack-usage`, which is the only way to see this
without a board:

```
parse_command(const char*, size_t, const char*, Command&, Refusal&)   14712 bytes
clear(Command&)                                                       14576 bytes
```

and before the grammar was made smaller, four times that. A frame that deep runs off the
end of the stack and into the top of the heap. The reason nothing ever went wrong is that
there was no heap up there to hit: an RP2350 with 400 kB free has its allocations far
below, so the zeros landed on memory nobody was using. On an RP2040 with 99 kB — the
`pico-w-sensor` profile — that is a different sentence, which is one more reason it is
**built only**.

Two changes, neither of them clever. The grammar now takes the eight sources this board
plans rather than the thirty-two the contract allows anyone: a ninth is refused by size,
which the hub already refuses earlier, and the structure is 14,568 bytes. And the one
command a node has in hand lives in one place — there is one loop, and nothing parses a
command while another is being obeyed — so no function holds one at all. `clear()` is a
`memset` through a `void*` behind two static assertions that say a command is a plain
structure, because the obvious way to write it is the way that built the temporary.

The same measurement is now part of building for a board. `-fstack-usage` writes a `.su`
file beside every object, `tools/stack_check.py` reads the ones belonging to this
firmware's own sources, and `make pico-device` fails when the deepest of them is over
1,536 bytes — which is where the deepest one is not, by a margin:

```
   1120  int main()                                                   (on an RP2040)
    848  bool sentry::read_provisioning(const char*, size_t, Provisioning&)
    840  bool vault::tear(sentry::Held)
deepest frame 1120 bytes of the 1536 one may have, over 268 functions
```

`main` is the frame at the bottom of the stack, never nested under anything; it is 448
bytes on an RP2350 and 1,120 on an RP2040, from the same source. The default plan used to
be in this list at 1,920 bytes, because it builds a source to hand to `Plan::take`; that
one is now a `static` in a function that runs once at boot.

On the board, with nine sources pushed down the cable by hand — the hub will not send them,
so the cable is the only way to ask:

```
command {"command_id":"eb5ac938-…","action":"configure","node_id":"pico-cablato",…}
# not a command for this node: too_many
# plan=3 revision=5      (unchanged: it was refused before anything was touched)
```

and with eight, which is the other side of the same boundary:

```
# plan=8 revision=98
```

### A rule, fired from a pin here

The last thing `PICO-04` asks for is a sensor on this board setting off a rule on the hub.
That was done on the live hub, in test mode, so the rule's actions were written down rather
than run, and with the hub's own camera rule switched off for the duration — arming loads
the detector only for the rules that need one, and there is no camera on that hub to load.

The rule watched `pico-ingresso.pir-1` for `sensor.motion` on a rising edge. The pin was
driven low, then high, from the serial console:

```
drive 15 0
# pin 15 (pir-1) driven low
drive 15 1
# pin 15 (pir-1) driven high
```

and on the hub, seconds later:

```
{"kind": "triggered", "message": "sensor.motion from pico-ingresso.pir-1: True.", "rule": "Movement at the entrance"}
{"kind": "would_run", "message": "Telegram: Movement at the entrance.", "rule": "Movement at the entrance"}
```

`would_run` is test mode saying what it would have done. The path it proves is the whole
one: a pad on the chip, debounced and settled by `input.cpp`, published as an event at
QoS 1, taken in by the satellite service, written to the journal, marked eligible, and
handed to the engine that decides what to do about it. Everything after that — sending the
message rather than logging it — is the same code the cameras use and is not this
firmware's.

Two things had to be fixed on the hub side before this could run at all. The saved rules
file was still in the first version of the rules format, which has no sensor triggers in it;
`scripts/migrate_satellites.py --apply` converts it, keeping a checksummed backup. And
arming is a POST with no body, which the hub says plainly if it is given one.

### What it keeps, and what it forgets

`PICO-02` asks for a versioned flash layout with two configuration slots, a provisioning
record that survives a boot, a recovery path over USB, and an interrupted write that still
leaves a valid configuration behind. All of that is now on the board.

The layout is in `sentry/vault.h`, which is pure and host-tested: five things a node keeps
— its identity, the authority, its certificate, its private key, and the last configuration
the hub sent it — with two slots each, one erase sector apiece, at the end of the flash
part. Nothing of it is in the image. The UF2 is the same on every board; what makes a board
this node is what it was told over the cable and wrote down. The version of the layout is
the record version in `store.h`, carried in every slot: a record written by a firmware that
laid the vault out differently fails to read rather than being taken for one of these.

```
keeping
# vault at 0x003f3000, 40960 bytes, 10 slots of 4096
#   identity      kept, write 1
#   authority     kept, write 1
#   certificate   kept, write 1
#   key           kept, write 1
#   configuration kept, write 1
```

Names and write counts, never values. The authority would be harmless to print and is not
printed either, because a rule with an exception in it is a rule somebody edits later.

**The last sector of the flash is not ours.** The vault used to end where the part ends.
Writing two configurations took the counter to `configuration kept, write 8`; a `.uf2` was
then copied on over BOOTSEL, exactly as the recovery list above says to, and the board came
back with `# running what it was left with: revision 101, 1 sources` and `configuration
kept, write 7`. The newest record was gone and the one in the other slot came through —
which is what two slots are for, and is not what they are for. The SDK agrees in its own
way: `PICO_FLASH_BANK_STORAGE_OFFSET` is where BTstack keeps the keys of anything a board
has paired with, it is two sectors at the end of the part, and on an RP2350 it sits one
sector lower still than on an RP2040. Three different things believed they owned that
sector. So the vault moved down three of them — `kTailNotOurs` in `flash_vault.cpp`, from
`0x003f6000` to `0x003f3000`, three sectors out of a thousand — and on a board with a radio
a `static_assert` checks the SDK's own address for that bank against where this vault ends,
rather than trusting it to stay where it was the day this was written. The same test run
again, on 2026-09-17: `configuration kept, write 4` before the UF2 copy, `write 4` after it,
and every other counter unchanged.

**A slot is a place, and a place can be given a new meaning.** Moving the vault moved every
slot three sectors down, which put each kind of record where a different kind used to live.
The board read them. A PEM out of the wrong slot is still a PEM, and the first boot on the
new layout reported `authority kept, write 6 / certificate kept, write 5 / key kept, write
2` — counters from records the previous layout had written for something else. A record now
carries one byte that says what it is, the reader is told what it expects to find, and a
mismatch is refused the way a bad checksum is; the record version went to 2 at the same
time, which is what makes every record from before this unreadable rather than
reinterpreted. That costs a board its identity once, over a cable, and buys the rule that a
slot never answers a question about a different slot. On the wired board: ten empty slots
after the flash, then `write 1` on each as it was provisioned again.

**A board given a new name.** The configuration in flash is addressed to a node, and the
parser that reads it back at boot is the one the hub's commands go through: a configuration
written for `pico-ingresso` is refused on a board that is now `pico-cablato`, with
`not_for_this_node`. That refusal was correct and useless — the board came back with no
plan, every boot, and said so in a line nobody was there to read. Two things follow from
it now. Provisioning a board under a different name gives back the pins, sets the revision
to "nobody has told me", and empties the configuration slot, because the plan running on it
belongs to the node this board has just stopped being:

```
provision {"node_id":"pico-prova","mqtt_host":"…","mqtt_port":8883}
# this board has a new name: what the last one was running is not kept
# provisioned as pico-prova, broker …, network none
status
# plan=0 revision=-1
```

And a board that already has such a configuration in flash — from a firmware that kept it,
or a name changed some other way — forgets it at the boot that refuses it, rather than
refusing it again every morning. Only when the board knows its own name: an identity slot
that did not come back makes every stored configuration look like somebody else's, and
that one is kept. Watched on the hardware on 2026-09-17: the stale slot was dropped at
boot, the hub sent a configuration for the name the board actually has, and the reset after
that came back with `# running what it was left with: revision 1, 2 sources`.

**A reset it was not asked for.** `PICO-01` wants a watchdog that has been seen to fire, so
there is a verb that stops feeding it:

```
hang
# not feeding the watchdog; this board should reset in about 8000 ms
--- the cable is quiet; waiting for the board to come back
--- the port went away after 3.8s
--- back after 6.4s
# ready pico2_w node=pico-ingresso boot=c4357001-… connection=602dfff3-… wireless=up reset=watchdog
# joining GL-SFT1200-fda
# joined GL-SFT1200-fda as 192.168.11.156, -46 dBm
# it knows the time and has what it needs: connecting on its own
# the network says it is 2026-09-17T16:38:37.491Z (answer 1)
# the broker accepted this node
# online, as pico-ingresso
# grant 6d92c2a8-…: applied
```

Everything after the reset happened with nobody at the cable: the node read its own name,
its three PEMs and a configuration of three sources back out of flash, joined, waited to be
told the time — a certificate has dates on it, and a board that does not know the time
cannot tell an expired one from a good one — and then connected. The boot id is new and the
sequence starts again at zero, which is exactly what tells the hub this was a reboot and
not a replay; the node id is the same one the hub registered. `reset=watchdog` is what it
says about how it got here, in `status` as well as at boot, because a board that keeps
coming back this way is a board with something wrong with it.

**An interrupted write.** The only way to see the two slots earn their keep is to interrupt
one, so there is a verb for that too. `tear` writes a record whose header promises three
pages and whose flash holds one — a sequence number a thousand higher than the current one,
so a node that compared them without checking them would choose it:

```
tear configuration
# half a configuration written to the slot that is not in use: unreadable, as an interrupted write is
hang
…
# ready pico2_w node=pico-ingresso boot=… reset=watchdog
# plan=3 revision=5
#   board-temperature board measure=temperature every=30s readings=2
#   pir-1 gpio pin=15 level=low state=off readings=1
#   light-1 adc pin=26 output=ratio every=20s readings=3
```

Revision 5 and all three sources: the torn slot lost, and it lost on its checksum rather
than on its sequence number. That is `T21`, on the board.

**No credentials, and no way around it.** The recovery verb erases what is kept, out of
flash and out of memory both:

```
credentials {"key": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"}
# that is not a set of credentials this firmware can use
forget authority
# forgetting the authority: gone
# this board is still provisioned; it will come back this way after a reset
connect
# no certificate to connect with; send: credentials <json>
```

A certificate offered as a private key is refused whole, and the key already in flash is
untouched — `keeping` still shows one write. With the authority gone, a new connection is
refused before anything is opened, and after a reset the board comes back as itself, still
knowing what it was told to run, with `link=down` and `credentials=no`: it does not join,
does not ask the time, and does not try. There is no setting anywhere that would let it.

One thing this does not defend against, and cannot: a board somebody walks away with gives
up its private key and its Wi-Fi passphrase to anybody with the patience to read its flash.
That is a property of the part, not of this firmware. It is why the satellite gets the
guest network and a certificate of its own — one the hub can revoke.

### What each source last read

A health message used to say how many readings a source had taken and whether its driver
was running, and nothing about what it measured. The hub's page has a column for the last
reading, and for this node it was empty until the next event happened to arrive — which for
a pin that nobody walks past is never.

It now carries the same three fields an event does, written by the same code:

```
"light-1": {"readings": 41, "driver": "adc", "last_reading_age_seconds": 9.8,
            "last": {"value": 0.1470, "unit": "ratio", "quality": "valid"}}
```

A source that has read nothing has no `last` at all, rather than a null with a unit beside
it: a bus that answered with nothing and a source nobody has asked yet are different things
and the page has to be able to tell them apart.

One source answers from somewhere else. A wire only publishes when it changes, so its last
event may be an hour old and may have been taken while the sensor was still settling — that
reading is honestly `unknown`, and a page showing it would say `Unavailable` about a pin
that is working perfectly. Health is asked how the pin is *now*, and the loop reads it every
turn, so a `gpio` source answers from the debounced input: the same value if nothing has
moved, an age of zero because it was just read, and a quality that stops saying `unknown`
once the sensor has had its settling time.

### A source kept without being read

The hub's page says, under the editor: *Set `"enabled": false` to keep a source in the
list without reading it.* Sending that to this board used to come back as
`failed: a gpio source has no enabled` — the word is a field of every source rather than an
option of any driver, and this firmware had only ever seen it as an option it did not know.

It is now what it says it is. A source that is switched off is planned, validated and
reported, and nothing else: it takes no pin, starts no driver, is never sampled, and stands
in nobody's way.

```
#   board-temperature board measure=temperature every=30s readings=1
#   pir-1 gpio disabled
#   light-1 adc pin=26 output=ratio every=20s readings=1
```

```
$ drive 15 1
# nothing in the plan is on pin 15
```

Its pin is still judged — a switched-off source on GPIO 25 is refused for naming the LED,
not accepted and then refused the day somebody switches it on — but it is not claimed, so
two sources may name one pin as long as only one of them is being read.

Switching one back on found a real fault, on the board and not in a test. A pad that has
been left with no function and no pull on it floats, and on an RP2350 it can float up and
stay up: `pir-1` came back with its pull-down enabled and read **high**, and stayed high
for as long as it was watched, while the same pin read low on a fresh boot and across a
configuration that never let go of it. A page would have shown motion at a sensor that is
not there. So a gpio source now drives the level its own configuration calls idle onto the
pad for ten microseconds before handing it to the input — `pull_down` is already a claim
that the wire idles low, and this asserts what was claimed rather than guessing at the
wiring. Measured, on the board, before and after:

```
--- off, then on, with the pad floating in between
#   pir-1 gpio pin=15 level=high state=on readings=1     (before)
#   pir-1 gpio pin=15 level=low state=off readings=1     (after)
```

### Installed and configured from the hub

`PICO-05` asks for a node whose profile can be installed and configured from the hub
without a terminal open on it, and for the page to show what such a board actually has:
its pins, its clock, its memory, why it reset, what drives each source and what it cannot
be asked for. That is now what the card shows, and everything on it came from the node
rather than from a guess about what a node like this would say.

What the hub will not send has moved earlier: the catalogue's limits are the firmware's own
— eight sources, about two kilobytes of configuration — and a driver this build does not
have, a source that names no pin, an option its driver has no place for, a list too long or
a configuration too large are all refused on the page instead of after a round trip. The
node checks all of it again and has the last word; nothing here trusts the hub's copy.

Measured against the running hub, for a node that has been configured only over MQTT:

```
400 {"error": "9 sources: a pico-2w-sensor takes at most 8"}
400 {"error": "the configuration is 4361 bytes; a pico-2w-sensor holds 2048"}
400 {"error": "pir-1: a gpio source needs pin, and this one names none"}
400 {"error": "pir-1.interval_seconds: a gpio source on a pico-2w-sensor has no interval_seconds"}
```

### Why it came back

A restart is evidence. A board that comes back because somebody unplugged it and a board
that comes back because its own loop stopped turning look the same from the hub — it is
there again — and they are not the same thing to whoever has to fix it. The chip knows
which it was, in a register that survives the reset, and the firmware read it only to print
it on a serial line that nobody is holding.

It is now read once at the top of `main()`, before the watchdog is turned on again, and
carried in every heartbeat:

```
"board": {"uptime_seconds": 8.0, "temperature_c": 36.4, "memory_available_kb": 352,
          "reset": "watchdog"}
```

The words are `power`, `brownout`, `button`, `watchdog`, `software` and `debugger`. A chip
that cannot tell leaves the field out: there is deliberately no `unknown` in the contract,
because a firmware given the word would write it where saying nothing is the truthful
answer, and a page showing it would report an answer nobody gave.

Two of them are read from the watchdog and the rest from one register, which is in a
different place with different bits on the two chips — `VREG_AND_CHIP_RESET` on RP2040,
`POWMAN_CHIP_RESET` on RP2350, in `src/device/reset_reason.cpp`. The RP2350 can tell a
brown-out from a power-on and an RP2040 cannot, so an RP2040 that browns out says `power`,
which is what it knows rather than what happened.

Run on the board, against the running hub:

```
# heap_free=352kB uptime=50s reset=power          (after the firmware was copied on)
# not feeding the watchdog; this board should reset in about 8000 ms
# heap_free=352kB uptime=8s reset=watchdog        (8 seconds later, by itself)
```

and on the hub, without a cable, a few seconds after that:

```
{"uptime_seconds": 36.2, "temperature_c": 36.4, "memory_available_kb": 352,
 "reset": "watchdog"}   1 restart, last 39.3 s ago
```

`button`, `brownout`, `software` and `debugger` have not been produced on a board: nothing
here presses the RUN pin, sags the supply or reboots itself, and they are written down as
the chip's own bits rather than as something that has been seen.

### The time moved under it

`T16` asks what a node does when the wall clock jumps. It had never been asked on hardware,
and asking it found something. A board reading its own temperature every five seconds was
told, over the cable, that it was an hour later than it had thought. Nothing complained.
Twelve readings went to the hub stamped an hour into the future, the hub wrote every one of
them down as `synced`, and when the bridge's next time frame put the clock back where it
belonged the thirteenth reading was stamped an hour *before* the twelfth — same node, same
source, sequence numbers going up, timestamps going down, and every one of them claiming to
be as good as a timestamp gets:

```
(26, 'board-temperature', 'synced', '2026-09-17T23:03:06.258000+00:00')
(27, 'board-temperature', 'synced', '2026-09-17T22:03:11.257000+00:00')
```

The timestamps themselves are not the problem, and they are not something this node can fix:
it wrote down what it was told, and an hour later it was told something else. What it can
fix is the claim. `Timebase::sync` now compares the offset it is about to keep with the one
it is replacing, and a move of more than two seconds — far more than a crystal loses between
two answers, far less than a timezone — is a step rather than a correction. What is already
in the queue was stamped against the offset that has just been thrown away, so it stops
saying `synced`; the stamps stay, because changing them would invent a moment nobody
measured. On the board, both ways in the same second and a half:

```
time 1789686457932
# time taken: 1789686457932
# the time moved by 3600003 ms: what is still queued no longer claims to be synced
# the time moved by -3600003 ms: what is still queued no longer claims to be synced
```

The second line is the bridge's own fifteen-second time frame, putting it back. A node that
is up to date has an empty queue, so on a healthy link the sweep finds nothing to do and the
line is all that is seen; what it does to a queue that is not empty is `test_spool.cpp`,
because a bridged node's queue never fills — see below.
### Whether somebody is here

`PICO-06` asks for BLE presence on the same board that is already keeping a TLS connection
open, and for it to mean the same thing it means on a Linux node. Both halves are now true,
and the second one is the harder one: the hub must not be able to tell which kind of node an
arrival came from.

So the rules are the agent's, written again in `src/core/presence.cpp` and tested against
the same cases: a few sightings inside a window make an arrival, sightings closer together
than a second count once, a long quiet with the radio listening the whole time makes an
absence, and a scanner that stops — for any reason — makes the state **unknown at once**
rather than absent. What is watched is one named device, by address or by iBeacon; "some
device appeared" is not presence, it is a bus going past the window.

The radio half is `src/device/ble.cpp`: BTstack on the same CYW43 the Wi-Fi is on, scanning
passively, leaving every advertisement in a queue the loop drains. Nothing is matched there
and nothing is decided there. BTstack's flash storage is deliberately not initialised —
this node never bonds, and the two sectors that code would take sit at the end of the
part, next to the vault — `kTailNotOurs` leaves them alone and a `static_assert` says so.

Measured on the board, with a Raspberry Pi advertising an iBeacon at the other end of the
room, while the same board kept its broker connection up:

```
#   beacon-hall ble e2c56db5-dffb-48d2-b060-d0f5a71096e0 present rssi=-52 sightings=173 weak=0 missed=0 readings=2
#   nobody-home ble AA:BB:CC:DD:EE:FF absent rssi=none sightings=0 weak=0 missed=0 readings=2
```

and on the hub, with no cable:

```
beacon-hall ble ready {"value": "present", "unit": null, "quality": "valid"}
nobody-home ble ready {"value": "absent",  "unit": null, "quality": "valid"}
```

The beacon was then switched off and back on. Fifteen seconds of silence is an absence, and
two sightings are an arrival; neither is a baseline, because neither came from unknown:

```
event beacon-hall 'absent'  valid        (the Pi stopped advertising)
event beacon-hall 'present' valid        (and started again)
```

`deafen` stops the scanner on purpose, the way `tear` writes half a record and `hang` stops
feeding the watchdog. It is the only way to see coverage loss without unsoldering an
antenna, and it is the case worth being sure of — a node that cannot hear has not found out
that the room is empty:

```
> deafen
# the scanner is off; the loop will ask for it again in about 5000 ms
event beacon-hall None unknown
event nobody-home None unknown
event beacon-hall 'present' valid initial     (the radio came back)
event nobody-home 'absent'  valid initial     (and the wait for absence started again)
```

Both of those are baselines: a state reached from unknown is where things stand rather than
something that just happened, which is the same distinction the agent makes and the same one
a PIR's first reading makes here.

`rssi_min` is a threshold on evidence, not a distance. Set to something the beacon in the
room cannot meet, the node says absent and says why in its own numbers — 230 advertisements
heard from a device two metres away, none of them counted:

```
#   beacon-hall ble e2c56db5-… absent rssi=none sightings=0 weak=230 missed=63 readings=2
```

What the hub refuses before sending, measured against the running hub:

```
400 {"error": "ghost: a device is watched by its address or by its iBeacon, not neither"}
400 {"error": "ghost: a device is watched by its address or by its iBeacon, not both"}
400 {"error": "ghost: ibeacon_major and ibeacon_minor are parts of an iBeacon, and this one watches an address"}
400 {"error": "ghost.pin: a ble source on a pico-2w-sensor has no pin"}
```

A board without the radio has no watch to offer: `ble` is refused at configuration time with
`ble needs a board with a radio, and this one has none`, and the image for such a board has
no BTstack in it at all.

### Something was loud

`PICO-07` asks for a microphone on the board and for an activity event to come off it. A
microphone here produces `audio.activity` — `true` when it has been loud for long enough,
`false` when it has been quiet for long enough — and that is what it produces whether or not
anybody is listening. Sending the sound itself is `PICO-08`, below; this increment is the
judgement, and the judgement is made on the board in every case, because a node that only
knew it had been loud while somebody was watching would not be a sensor.

The judgement is the agent's, written again in `src/core/acoustic.cpp` and tested against
the same numbers: the level of a block in dB full scale, a threshold, `activity_min_seconds`
of it before anything is said, and six decibels of hysteresis below the threshold for
`activity_hold_seconds` before it is taken back. Bursts do not add up — a shout, a second of
quiet and another shout is not a second of noise — and the band between the threshold and
the hysteresis is a band nothing happens in, which is what keeps a level sitting on the line
from flickering. Eleven tests drive it with synthetic tones: a full-scale sine measures
−3.0 dBFS, silence measures −96.0, and a block of samples turns into an event at the end.

The capture is `src/device/i2s.pio` and `src/device/i2s.cpp`: one PIO state machine clocking
a Philips I²S frame — 32 bits a channel, the word select turning on the falling edge that
begins the last bit, which is the one bit that decides whether a microphone is a microphone
or a noise generator — and two DMA channels that start each other so that one is always
filling a block while the loop reads the other. Each buffer wraps on itself in hardware, so
a channel whose turn comes round again writes at the beginning of its own block and can
never write past it; a block caught being overwritten is dropped and counted rather than
measured, because a level taken from half one block and half the next is a level of nothing.

That pairing is not decoration. The first version used one channel and started it again each
time the loop took a block, and the board said so at once — every single block had lost the
sound of the gap before it:

```
#   hall-noise microphone pin=6 clock=7 left level=-96.0 quiet blocks=223 missed=223
```

With the two chained, on the same board in the same minute:

```
#   hall-noise microphone pin=6 clock=7 left level=-96.0 quiet blocks=1866 missed=2
```

1866 blocks of 512 frames at 16 kHz is 59.7 seconds of sound in the sixty seconds it ran,
and the two it missed were the moment the configuration arrived. Left running, it stayed
that way: 7250 blocks four minutes later — 232 seconds of sound in 232 seconds — with the
same two, while the board kept its broker connection up and watched a beacon at the same
time.

Time is counted in the sound that was actually heard rather than on the clock. A block this
board never captured is not silence, and a hold that timed out across a gap would be the
node deciding a room went quiet during the one stretch it could not hear it — the same rule
the watch above follows when its scanner stops.

The data line is pulled down, which is worth saying because it is what the numbers above
are. A MEMS microphone drives its half of the frame and lets the other half go; for that
half the line is held by nobody, and floating it reads as whatever the mains put on the pad.
Before the pull-down this board measured −90.3 dBFS with nothing attached to it, which is
exactly one least significant bit, which is exactly a wire. After it, −96.0, which is the
floor and the honest answer for a board with no microphone on it.

What the hub refuses before sending, measured against the running hub:

```
400 {"error": "kitchen-noise: a pico-2w-sensor listens to one microphone, and hall-noise is already it"}
400 {"error": "hall-noise.alsa_device: a pico-2w-sensor has no filesystem and no audio stack"}
```

The first of those is the board's own limit said early: one state machine is clocking I²S
and one pair of buffers is behind it, so a second microphone would share the first one's
clock and read the first one's pin. The firmware refuses it too, by name.

The three pins are given back when the plan changes, unlike the radio, because a clock left
running on a pin nobody is listening to is a microphone that looks switched off and is not.
Reconfigured from the microphone to three plain inputs on the same pins, the board:

```
#   was-the-clock       gpio pin=7 level=low state=off readings=1
#   was-the-data        gpio pin=6 level=low state=off readings=1
#   was-the-word-select gpio pin=8 level=low state=off readings=1
```

**No microphone has ever been attached to this board.** Everything above was measured with
three pins and nothing on them: the frame is clocked, the blocks arrive without a gap, the
silence is silence and the events reach the hub. What has not been checked against hardware
is the one thing hardware would settle — whether the bit this program samples is the bit the
microphone meant, whether the channel is the channel and whether a loud room reads as a loud
room. The PIO program is written to the Philips timing and the arithmetic is tested against
synthetic tones, and neither of those is a microphone.

### Sound the hub can hear

`PICO-08` is the other half: a browser opens `pico-ingresso.hall-noise` and the board sends
the sound while it is open. It is a second TLS connection, made when somebody starts
listening and dropped when nobody is, and the broker's connection is never touched to make
room for it — the lease, the commands and the events go on over the first one while the
second is carrying audio.

What goes over it is the same format a Pi sends, written in `src/protocol/audio.cpp`: a
thirty-byte header — `SMA1`, the version, a flag that says a block was lost before this one,
a sequence, the sample this block starts at, when it was captured, and how many samples it
carries — and then the samples themselves, signed 16-bit little-endian at 16 kHz. Eight
tests cover the writer, and `tools/audio_check.py` feeds what it writes to the hub's own
`Reassembler`, a byte at a time as well as whole, so the check is against the reader that
will actually read it rather than against a second opinion written here.

Measured on a Pico 2 W against the running hub, listening through `/api/audio/monitor`:

```
{'blocks': 271, 'duplicates': 0, 'gaps': 0, 'node_gaps': 0, 'lost_samples': 0, 'filled_samples': 0}
# audio=idle blocks=271 dropped=0 connections=1 last=the hub asked it to stop
# credentials=yes socket=open mqtt=online queued=0 coalesced=0 dropped=0
```

271 blocks of 1600 samples is 27 seconds of sound in the 30 the listener was open, the rest
being the dial and the handshake; the hub counted every one of them and had to invent
nothing. Then a run of 97 seconds across four renewals, five connections in a row, and the
hub killed in the middle of a stream: the board kept its broker connection, let the grant
expire, queued its events, and picked the microphone up again when the hub came back. Free
heap stayed at 307 kB throughout.

Three things had to be fixed to get there, and all three were found by the board rather than
by a test:

**The hub would not speak 1.2.** `media_gateway.py` requires TLS 1.3 and this firmware had
only 1.2 compiled in, so every attempt ended in `UNSUPPORTED_PROTOCOL` before a byte of
sound. The plan's rule for this gate is that the hub's security does not come down to meet a
microcontroller, so 1.3 went in instead: `MBEDTLS_SSL_PROTO_TLS1_3`, ephemeral key exchange
only, PSA for the key schedule, and the four library sources the SDK's own CMake leaves out.
It cost 28 kB of flash and 2 kB of RAM. The first handshake then died at the encrypted
extensions with `ChangeCipherSpec invalid in TLS 1.3 without compatibility mode` — OpenSSL
sends that empty record so that middleboxes written for 1.2 let the connection past, and a
client has to be told to expect it.

**A write bigger than a record stopped the program.** With 1.3 up, the first block reached
`mbedtls_ssl_write`, which took one record's worth and said so, and the layer between
mbedTLS and lwIP treats a short write as impossible: `*** PANIC *** ret <= 0`, and a board
that rebooted every time the hub asked for sound. Nothing here hands it more than a record
will hold now, and under 1.3 that is less than the buffer — the record also carries the real
content type and padding to the next step of sixteen. An MQTT packet has to fit in one
record because it goes whole or not at all, and a `static_assert` in `net.cpp` says so
against `kMaxPacketBytes` rather than leaving it to be discovered again.

**The sequence restarted under the hub's feet.** Blocks are captured while the connection is
still being made, so some are already queued when the gateway answers; the code reset the
sequence to zero at that moment, and the hub saw three blocks it had already counted followed
by a hole where they should have been. The count belongs to the stream, not to the
connection — a connection that drops and is remade gets a fresh reader at the other end
anyway — so it is set once, when the stream starts. Before and after, on the same board:

```
{'blocks': 236, 'duplicates': 6, 'gaps': 2, 'lost_samples': 9600, 'filled_samples': 9600}
{'blocks': 236, 'duplicates': 0, 'gaps': 0, 'lost_samples': 0,    'filled_samples': 0}
```

The same run found one on the hub: a connection's counts were added to the totals twice,
once by the gateway closing the sink and once by the stream that asked for it, so every
number in `/api/microphones` was doubled. A sink now hands its count over and keeps an empty
one, so it is counted once however many times it is closed.

**Still no microphone attached.** The sound above is the silence of three bare pins, sent
end to end and reassembled. Everything about the transport has been measured; nothing about
what a microphone would put on the wire has.

### A board with no radio

`PICO-09` asks for the other kind of Pico to be a satellite: one without a W in its name,
which cannot reach a broker at all. It reaches the machine that powers it instead, over the
USB cable, and that machine — running `scripts/pico_bridge.py` — carries its messages the
rest of the way.

What crosses the cable is in `contracts/satellite/v1/bridge.json`: a magic, a version, what
the frame carries, a counter, a length, the payload and a CRC-32 over all of it. The kind is
also the direction, so a node that sent a command or a bridge that sent an event is refused
rather than believed. `tools/link_check.py` runs both ends against each other in `make pico`
— the firmware frames by hand at offsets it decides for itself, the bridge uses `struct` and
`zlib`, and neither has read the other.

The board has one USB port, so **the console goes down it too, inside frames**: `said` is a
line the board printed, `typed` is a line somebody sent it. The alternative was text and
frames sharing the channel unmarked, where a line of diagnostics can be read as a frame and
a frame lands in somebody's terminal. `bridge.cpp` registers a stdio driver, so `printf` and
`getchar` work exactly as they do on a board with a serial monitor, and provisioning a wired
board is the same conversation it is on a wireless one.

The messages themselves are written by the same functions as on a board with a radio —
`serve_the_bridge` climbs the same ladder as `serve_the_broker`: the announcement first,
then what the hub is waiting for, then a reading, and only if the lease allows it. Two
things differ. There is no acknowledgement to wait for, so a frame written to an open port
finishes that message; and there is no session to negotiate, because a bridge saying hello
*is* the connection. The lease, the spool, the order and the goodbye are unchanged.

**A Pico 2 was run against this hub on 2026-09-17** — as `pico-cablato`, on the same board
as `pico-ingresso` with its radio never brought up. It was provisioned over the cable,
announced itself, was granted, was configured from the hub twice, and reported board
temperature, a GPIO and a microphone's activity into the journal. Over four and a half
minutes: 54 frames out, 12 in, `discarded=0 missed=0 refused=0`, `heap_free=401kB` — a
hundred kilobytes more than the same board with the TLS stack in it.

Three things were tried on purpose:

- **A board claiming to be another node.** Before it was provisioned it announced itself as
  `pico-ingresso` on a port mapped to `pico-cablato`. Nothing was published, and the bridge
  said what it had refused. That is the only identity check there is on this link: a USB
  serial number is a string a device chooses for itself.
- **The bridge killed with `SIGKILL`.** The hub saw the node go offline two seconds later,
  from the will the bridge had registered as that node — which is why the bridge waits for
  the board's first announcement before connecting, since until then there is no connection
  id for a will to name.
- **Ninety seconds with nobody on the cable.** The board went on reading, held what it had,
  and delivered it when the bridge came back on a new connection id. Nothing was dropped and
  the configuration survived.

**The first words, which used to be lost.** A board with a serial monitor prints its boot
lines to whoever is watching; a bridged board has nobody on the cable until the bridge
opens the port, which is a second or two after the board has already said everything
interesting about how it came back. Those lines were dropped on the floor, and the one time
it mattered — a board coming back with no plan — the explanation had already scrolled past
before anything could read it. `bridge.cpp` now holds the first kilobyte of what the board
says until the port is opened, and sends it first when somebody is there:

```
2026-09-17 23:18:17,465 INFO pico-cablato: /dev/ttyACM0 is open
2026-09-17 23:18:19,000 INFO pico-cablato | # provisioned as pico-cablato, broker …, network none
2026-09-17 23:18:19,000 INFO pico-cablato | # credentials: read back from flash, all three
2026-09-17 23:18:19,000 INFO pico-cablato | # the configuration in flash was left
                                          by another node: forgetting it
2026-09-17 23:18:19,000 INFO pico-cablato | # ready pico2 node=pico-cablato … reset=power
```

A kilobyte, and then it stops holding: what the board says after that is a board that has
been running for a while, and a buffer that grew to fit it would be a buffer that eats the
heap on a board nobody ever plugs into. Lines that did not fit are counted as unsent, the
same as any other line the cable would not take.

**What the hub is told, and by whom.** The board's retained state carries
`reached_by: bridge`, and `/api/satellites` shows it. The node says it because the node is
the only one that knows; the bridge forwards what it is handed and writes nothing into it.
The two wired platforms are in the catalogue as `pico-wired` and `pico-2-wired`, with no
`ble` driver and no stream: there is no Bluetooth on the chip, and the hub hears a
microphone over a second TLS connection the board makes, which this board cannot make.

**A broker that came back.** The bridge owns the MQTT connection, which means the board
never learns anything about it: the cable did not move, so as far as the firmware is
concerned nothing happened. The broker was killed and restarted underneath one on
2026-09-18, and every retained message went with it — including the node's own presence,
which is the retained `state` the hub reads `online` from. The bridge reconnected, resumed
its subscription, went on forwarding readings, and the hub called the node offline and
stopped writing them down. Fifty seconds of temperatures went nowhere and nothing said so.
The board could not have told anyone: it was not there. So the bridge says it again — the
last announcement it was handed, byte for byte, republished retained on every connect — and
it says nothing at all when the board has never announced itself, because a bridge that
invented a presence would be worse than one that lost it. The same run with the fix in:
the broker went away at `00:11:11`, came back at `00:11:42`, and the journal's next row is
`sequence 86` at `00:12:04` with nothing missing in between.

**A queue that never fills.** One consequence of the bridge owning the connection is that
the board's spool does not: a frame written to an open port *is* the delivery, so the
reading leaves the spool as soon as it is handed over and the queue reads zero however long
the broker has been away. Killing the broker for forty seconds left `queued=0` and
`unsent=0` on the board and the readings piled up in the bridge instead. What a full spool
does is tested on host and has been watched on a board with a radio, which is the one that
has a queue of its own to fill.

### Taken away, and given back

`PICO-11` asks for the revocations to be watched rather than assumed, and watching them
found one. `satellite_admin.py revoke` prints "the hub refuses this node either way",
because reloading a broker's access control does not close the connections it already has —
so the hub is the layer that has to notice. It did not. `authenticate` looked at the
registry file again only when the record it had cached was already a refusal, which catches
a node approved a moment ago and misses a node revoked a moment ago. A revoked node went on
publishing into the journal, and would have until the hub was restarted.

    22:22:39  satellite_admin.py revoke --node pico-cablato
    22:23:37  board.temperature  recorded    (from a node that had been revoked a minute ago)
    22:24:07  board.temperature  recorded

The fix is one line and one stat per message: ask the file every time, in both directions.
`test_a_node_revoked_from_the_shell_stops_being_believed_by_a_hub_that_is_running` is the
test, and the rest of the revocation was then watched on the board:

    22:25:14  # revoke bc4fbef6-…: applied        the lease, answered by the node
    22:25:23  the hub restarted with the fix; nothing from pico-cablato recorded after this
    22:28:53  # grant=none … queued=7             the board keeps reading and holds them
    22:29:45  # online, as pico-cablato           it announces, and gets no session at all
    22:30:24  satellite_admin.py approve --node pico-cablato
    22:31:55  pico-cablato is online in epoch 105 with 3 sources

The gap between 22:30:24 and 22:31:55 is the last line of that transcript and is worth
naming: a node approved again while it is already connected stays offline until it says
hello again, because a session begins at a `state` message and the retained one was
delivered when the hub subscribed. Restarting the bridge — or the node — is what produced
the last line. A node approved for the first time does not have this problem, because it
connects after it is approved.

### Thirty-five minutes of nothing happening

The other half of `PICO-11` is the part with no findings in it. The wired board was left
running as `pico-cablato` while the rest of this was written, and asked what it had been
doing every so often:

    22:37:32  frames sent=207 unsent=0 read=43 discarded=0 missed=0 refused=0
              heap_free=401kB uptime=1647s queued=0 coalesced=0 dropped=0
    22:45:24  frames sent=283 unsent=0 read=57 discarded=0 missed=0 refused=0
              heap_free=401kB uptime=2119s queued=0 coalesced=0 dropped=0

Nothing moved that should not have: the heap is the same number half an hour apart, the
queue is empty because everything was delivered, and of 66,164 blocks of audio the only one
missed was missed at boot. The lease was renewed five times in that window without the node
noticing. Thirty-five minutes is not a week, and this is the longest this firmware has been
watched rather than the longest it has run.

### What goes out with an image

A `.uf2` on somebody's desk says nothing about itself, so a build can write down what it is:

    make pico-release PICO_BOARD=pico2_w        # build/pico2_w/manifest.json

Almost all of it is measured rather than declared — the commit, the SDK and the four
submodules under it, the sha256 and the size of the image, which half of the firmware was
actually compiled into it, what the hub's catalogue will let a node of that kind be asked
for, and the pins the board keeps for itself. The pins and the limits are asked of the
firmware rather than written down beside it: `tools/board_emit.cpp` prints what `pins.h`,
`plan.h` and `vault.h` say, and `tools/release_manifest.py` copies the answer.

The two halves are checked against each other on the way past, and a manifest is refused
rather than written when they disagree: an image with no TLS stack for a board the hub
would offer Bluetooth to, a hub that would send more sources than this firmware plans, or a
configuration larger than one vault slot holds. It also runs the host suites and the
cross-checks, so a manifest that says they passed is a manifest that watched them; with
`--no-checks` it says nothing about them rather than saying they passed.

One of them, in full:

    build/pico2_w/manifest.json: pico2_w as pico-2w-sensor, run on hardware
      1240064 bytes, sha256 addcc045092d08f8e84331c4973fb73ff07101a3e2717e4f084c53133f8db044
      from 8778c56e8fad
      sdk 2.2.0, rp2350-arm-s
      23 suites passed, 0 failed, cross-checks: all passed

What cannot be measured — how far each board and each capability has really been taken, and
what this firmware does not do — is in `release.json`, which is the one file to read before
telling somebody a board is ready. `tests/unit/test_pico_release.py` ties it to the things
it can be tied to: every board it names is a platform in the hub's catalogue, every
microcontroller platform the hub offers is a board somebody can build, and the boards are
the boards the workflow builds.

### Installing one, and getting it back

The whole of it, from a board out of its bag to a node in the journal:

1. **Build and flash.** `make pico-device PICO_BOARD=pico2_w`, hold BOOTSEL while plugging
   the board in, and copy `build/pico2_w/sentry_firmware.uf2` onto the drive that appears.
   A board already running this firmware does not need the button: opening its port at 1200
   baud puts it back in the bootloader.
2. **Give it a name and a key.** `satellite_admin.py node-key --node pico-ingresso` makes
   the key and the request, `sign` issues the certificate, and `register` puts it in the
   registry as waiting. The key never leaves the machine it was made on except over the
   cable, in the next step.
3. **Provision it over the cable.** `tools/provision_board.py` writes the identity, the
   authority, the certificate and the key into the board's flash, through the same console
   as everything else. `status` on that console says `provisioned=yes`.
4. **Approve it.** `satellite_admin.py approve --node pico-ingresso`, then `acl` to write
   the broker's access control and `kill -HUP` the broker. The node announces itself and
   the hub gives it an epoch.
5. **A board with no radio** skips the network and is carried instead: the same three steps,
   and then `scripts/pico_bridge.py --config <file>` on the machine it is plugged into, with
   that port mapped to that node id. See **A board with no radio**, above.

Getting it back is the same list, backwards, and each step is one somebody may need on its
own:

- **It is running the wrong firmware, or none.** Hold BOOTSEL and copy a `.uf2` on. Nothing
  in the vault is touched by flashing: the board comes back as itself, with its identity,
  its credentials and its configuration. A firmware that laid the vault out differently is
  the one case where they are lost, and it is deliberate — the record version changes and
  old bytes fail to read rather than being guessed at.
- **Its certificate is wrong, or expired.** `forget key` on the console overwrites it —
  `certificate` and `authority` are the other two, and any of the three leaves the identity
  alone — and `credentials <json>` puts a new set in. The board says `no certificate to
  connect with` in between and keeps reading its sensors.
- **It should not be trusted any more.** `satellite_admin.py revoke`, then `acl` and a
  broker reload. The hub refuses it from the next message, as above; the broker stops it
  reconnecting. Both, because a reload does not close an open connection.
- **It is a different board now.** Register the new one under a new name and revoke the old.
  A name is never reused for another board: the fingerprint in the registry is what makes a
  certificate belong to a node, and reusing a name would make two boards one history.
- **Nobody knows what it is doing.** `status` over the console says what it is provisioned
  as, whether it has credentials, whether it is connected, what its clock is worth and what
  it is holding. On a bridged board the same line says whether the cable is open and whether
  a bridge has said hello.

## What is in here

| Path | What it is |
|---|---|
| `include/sentry/json.h`, `src/protocol/json.cpp` | A reader and a writer for the shapes the contract uses, in buffers the caller owns. No allocation, depth capped at 4, refusing rather than truncating. |
| `include/sentry/event.h`, `src/protocol/event.cpp` | The event envelope, written only after every field has been checked, so a refusal costs nothing and never leaves half an event in the buffer. |
| `include/sentry/command.h`, `src/protocol/command.cpp` | The closed command grammar: the actions, what each one may carry, and who it is addressed to. |
| `include/sentry/control.h`, `src/protocol/control.cpp` | The three control messages this node writes, including the goodbye that has to fit in a 255-byte will. |
| `include/sentry/mqtt.h`, `src/protocol/mqtt.cpp` | The packets, and what this node refuses to speak: no QoS 2, no length it has nowhere to put, no packet only a client may send. |
| `include/sentry/client.h`, `src/protocol/client.cpp` | What is in flight, what is owed and when to stop waiting: one publish at a time, sent again with DUP under its own packet id, a ping before the keepalive runs out, and a link given up on rather than waited on forever. |
| `include/sentry/topics.h`, `src/protocol/topics.cpp` | Where each message goes, built in one place because the topic is the one claim a node cannot make up. |
| `include/sentry/names.h`, `src/protocol/names.cpp` | What a name, a uuid, a kind, a driver and a timestamp are, in one place rather than in whichever file needed one first. |
| `include/sentry/session.h`, `src/core/session.cpp` | The order a connection comes up in: nothing is announced before the subscription is confirmed, and every connection has an identifier of its own. |
| `include/sentry/lease.h`, `src/protocol/lease.cpp` | Permission with an end to it, measured on this node's clock, and the memory of the last 64 commands answered. |
| `include/sentry/store.h`, `src/core/store.cpp` | The framing that lets an interrupted write be recognised as one, the byte that says what kind of thing a record is, and the rule for choosing between the two slots. |
| `include/sentry/vault.h`, `src/core/vault.cpp` | Where the five things a node keeps live: two slots each, a whole erase sector apiece, and the most each one may be. |
| `src/device/flash_vault.h`, `src/device/flash_vault.cpp` | The only file that writes to flash: erase, program, read back through the same path the boot takes, and a `tear` that stops halfway on purpose. |
| `include/sentry/identity.h`, `src/core/identity.cpp` | The provisioning record — the node id and where the broker is — and the boot id that must differ every boot. |
| `include/sentry/credentials.h`, `src/core/credentials.cpp` | The authority, the certificate and the key: what each one has to be, taken whole or not at all, and a `forget()` that overwrites the key. |
| `include/sentry/timebase.h`, `src/core/timebase.cpp` | A counter that wraps seen as one that does not, what a reading may claim about its own timestamp, and the difference between the wall clock being corrected and being stepped. |
| `tools/emit.cpp`, `tools/unpack.cpp`, `tools/mqtt_emit.cpp`, `tools/client_run.cpp` | Write events, packets, a whole connection and configuration slots for the cross-checks above. |
| `tools/stack_check.py` | How deep the deepest frame in this firmware is, read from what `-fstack-usage` leaves beside the objects. Part of every board build, because the stack is eight kilobytes and what is under it is the heap. |
| `src/device/onewire.h`, `src/device/onewire.cpp` | The 1-Wire bus, bit-banged on one pin: the reset, the slots, and interrupts off for the microseconds that decide what a bit was. The only part of this firmware that cannot be tested off a board. |
| `src/device/reset_reason.h`, `src/device/reset_reason.cpp` | Why the board is running this time, read from the watchdog and from one register that is in a different place on the two chips. Answers `unknown` rather than guessing. |
| `src/device/main.cpp` | The one program that runs on a board: USB serial, the LED, the die temperature, and the same event writer as everything else. |
| `include/sentry/spool.h`, `src/core/spool.cpp` | The queue for an outage: which readings collapse into a newer one, which are never dropped for them, and what is counted when something is given up. |
| `include/sentry/input.h`, `src/core/input.cpp` | What a wire may mean: the baseline that is not an intrusion, the settling window a PIR needs, the debounce, and the polarity software cannot guess. |
| `include/sentry/sensors.h`, `src/core/sensors.cpp` | Turning what a sensor returned into a reading or into an admission there is none: the 1-Wire CRC, the 85 °C a DS18B20 holds after a reset, and an ADC count nothing could have produced. |
| `include/sentry/plan.h`, `src/core/plan.cpp` | What a configuration is allowed to ask for: which drivers this firmware has, which options each one takes, and a refusal that names the source it failed on — decided whole, on a copy, before a pin is touched. |
| `include/sentry/pins.h`, `src/core/pins.cpp` | Which pins a configuration may use and who already has them, refused whole rather than in part. |
| `include/sentry/presence.h`, `src/core/presence.cpp` | Whether one named device is here: what counts as a sighting, what counts as an arrival, and why a scanner that stopped means unknown rather than absent. The agent's rules, and no radio anywhere in it. |
| `src/device/ble.h`, `src/device/ble.cpp`, `src/device/ble_none.cpp` | The scanner: BTstack on the CYW43, listening passively and leaving every advertisement in a queue the loop drains. Nothing is matched here. The third file is the whole of it on a board with no radio. |
| `src/device/btstack_config.h` | What of BTstack is compiled in: a scanner, no bonding, no flash storage — the sectors that would take are the ones the vault was moved off. |
| `include/sentry/acoustic.h`, `src/core/acoustic.cpp` | What a level is and what makes it an event: dB full scale over a block, a threshold with six decibels of hysteresis under it, and the seconds either side. The agent's numbers, and no pin anywhere in it. |
| `src/device/i2s.pio`, `src/device/i2s.h`, `src/device/i2s.cpp` | The microphone: a state machine clocking a Philips I²S frame and two DMA channels that start each other, so no sound is lost between one block and the next. A block is read once and handed straight back; nothing here keeps audio. |
| `include/sentry/audio.h`, `src/protocol/audio.cpp` | The shape sound leaves in: the thirty-byte block header, the line the gateway is greeted with, and the reading of its one-line answer. Checked against the published contract and against the hub's own reader. |
| `src/device/media.h`, `src/device/media.cpp` | The second connection: dial, greet, and send while the hub is listening. Four blocks of queue, the oldest unsent one dropped when it fills, and the hole marked so the hub can fill it. |
| `include/sentry/link.h`, `src/protocol/link.cpp` | What crosses the cable to a board with no radio: the frame, its counter and its CRC-32, and a reader that resynchronises on the magic rather than giving up. |
| `src/device/bridge.h`, `src/device/bridge.cpp` | The USB port on a wired board: frames out, frames in, and the console carried inside them so that no text ever shares the channel unmarked. |
| `tools/board_emit.cpp`, `tools/release_manifest.py`, `release.json` | What one build of this firmware is: the commit, the SDK, the hash of the image, the pins the board reserves — asked of the firmware — and the half no tool can measure, which is how far each board has really been taken. |
| `tests/` | The twenty-three suites, and a tiny harness rather than a test framework. |

Everything under `src/protocol` is pure: no SDK, no clock, no network, no allocation, and
no `malloc` to fail on a board with 264 kB. That is what makes the host build meaningful
rather than a stand-in, and it is the boundary the device half will be built against.

The protocol is compiled with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
-fno-exceptions -fno-rtti`: a change that needs exceptions or runtime type information
fails here, in a second, rather than at link time on a target with neither.

## What is not here, and what it waits on

- Four of the six reasons a board can come back. `power` and `watchdog` have been
  produced on the board and read on the hub; `button`, `brownout`, `software` and
  `debugger` are read from bits nothing here has set. An RP2040 cannot tell a brown-out
  from a power-on at all, and says `power` for both.
- Keeping the time, as opposed to getting it once. There is a rule: three hours with no
  answer from whatever tells this node the time and it stops calling what it stamps
  `synced`, without stopping stamping — the offset is still the best estimate it has, and
  `unknown` is the honest word for what it is worth. That rule has never been watched
  happening, and three hours is longer than any session here has spent waiting for it. The
  jump itself has now been done, both ways, and is written up above; what is still
  unexamined is the slow half — a board up for a day, the drift between two answers, and a
  network that goes away mid-interval.
- Two seconds is a number somebody chose. It is what separates a correction from a step,
  and it was picked from what a crystal plausibly loses between two answers rather than
  measured on this hardware over a long enough run to be sure. A board whose time source
  answers once an hour and whose crystal is worse than assumed would call an ordinary
  correction a step, which costs a queue its `synced` and nothing else; the error is in the
  safe direction, but it is an assumption and not a measurement.
- What is queued when the clock steps, on a board. The sweep is tested on host and the
  detection is watched on hardware, but a bridged node's queue is empty almost all the time
  — see the next point — so the two have never been seen together on a board. That needs
  the node with a radio, which is the one with a queue of its own.
- A bridged node does not own its connection, and does not know it has one. The bridge
  holds the MQTT session; a reading handed to the cable has been delivered as far as the
  firmware is concerned, so `queued` reads zero however long the broker has been away, and
  everything the board says about its own queue is about a queue that never fills. The
  broker going away and coming back is now handled at the bridge — it says the node's last
  announcement again on every connect — but it is handled *there*, and a board that is told
  nothing cannot be tested on what it does when told.
- What the radio build leaves on an RP2040. The same image takes 168,832 bytes of static
  RAM on a Pico W and 168,448 on a Pico 2 W — near enough the same firmware — but the first
  part has 270,336 bytes and the second has 532,480. That is 99 kB for the heap, the TLS
  handshake and the stack on one, and 355 kB on the other, which is the measured reason
  `pico-w-sensor` stays **built only** rather than a profile anybody is invited to run.
- Where the zeros of the command live. The one command a board holds is 14,568 bytes and
  it is in the image rather than in the section cleared at boot, because a structure whose
  every field has a default is initialised as far as the compiler is concerned: fourteen
  kilobytes of nothing, written into flash and copied out of it at boot. Flash is what this
  board has most of, and the alternatives are a section attribute the compiler refuses or a
  union with a constructor that does nothing, so it stays as it is and is written down here.
- A node approved again while it is already connected. A session begins at a `state`
  message, and the retained one was delivered when the hub subscribed, so a node that was
  revoked and then approved again stays offline in a hub that would now accept it — until
  it says hello again, which a reset or a restarted bridge does. Watched happening on
  2026-09-17, and left as it is rather than fixed in the same breath as the refusal.
- A power cut, as opposed to a reset. Everything above was proved with the watchdog, which
  resets the chip without taking the power off it. What a half-finished flash write does
  when the supply actually sags — the part is mid-erase and the voltage is falling — is not
  something a `tear` verb can stand in for.
- Why the last sector goes. It was watched going, twice, and the vault was moved off it and
  off the two the SDK's flash bank wants — but what erases it during a BOOTSEL copy is not
  something any document here names. Three sectors is a margin chosen from an observation,
  not a boundary read out of a datasheet, and a part or a bootrom that wants four would
  cost a configuration again before anybody noticed.
- Every driver in the hub's catalogue for this platform is now here: `board`, `gpio`,
  `adc`, `onewire`, `ble` and `microphone`. A configuration naming anything else — a
  BME280, a camera — is refused by name. So is `video.start`, and it will stay refused:
  there is no camera on this board and no encoder to put a frame through.
- How long a reading waited is measured now — the spool stamps each reading with the
  millisecond it arrived, and `queued_ms` is the difference when the frame is written, so
  the journal shows 5 ms on a quiet board and 13–19 ms in the moment after a
  reconfiguration, when the baselines are taken and several readings queue at once. A
  coalesced reading reports its own wait, not the one it replaced. The hub reads it now —
  `queue.waited` on the node, and one line when a node crosses two seconds and one when it
  comes back — which was watched happening on 2026-09-18 with three nodes at once, after the
  hub had been stopped for seven hours. What is still a guess is the two seconds.
- A revoked certificate, as opposed to a revoked node. Four certificates have now been
  offered to this broker and refused or allowed on purpose — another authority, an expired
  one, a genuine one for a name nobody registered, and one node's certificate publishing
  under another node's name — which is the rest of `T06` and most of `T07`; they are in
  [the hub's own document](../../docs/satellites.md#what-it-refuses-watched-rather-than-assumed),
  because it was the broker answering, not the board. What is left is a certificate listed
  as revoked: there is no CRL in this configuration, and revocation here is the registry
  and the access list. Offering any of these *from the board* is also still undone — the
  board has only ever been given credentials that work.
- A sensor with wires on it. A pin is read, debounced, settled and reported, and that path
  has been exercised on the hardware — but by the board driving its own pad, not by a PIR
  or a reed switch. What a real sensor does that a driven pin does not — the settling after
  power, the pulse a PIR holds, the bounce of a contact — has not been watched yet.
- A rule that runs its actions rather than logging them. The rule above fired in test mode,
  which is as far as this firmware's business goes: whether a message is actually sent is
  somebody's decision about their own house. Nor has a rule been fired by anything but a
  pin driven from the console — the sensor is still missing, above.
- A DS18B20 that answers. The bus is written, the conversion is timed, the scratchpad is
  read and the CRC is checked, but no probe has ever answered the reset on this board: the
  only 1-Wire transcript here is an empty bus. The pull-up is the chip's own, which is
  around fifty times weaker than the 4.7 kΩ the part asks for — enough for an empty bus to
  read as empty, not enough for a probe on a long wire.
- A watch on a phone rather than on a beacon. `address` is written, tested and refused
  correctly, but the only device this board has ever recognised is an iBeacon advertising
  from a Raspberry Pi. A modern phone rotates its address every fifteen minutes, which is
  exactly what an address-based watch cannot follow — which is why the agent takes a beacon
  too, and why nothing here claims otherwise.
- What a busy building does to the queue over hours. `missed` counts advertisements
  dropped because the loop did not come back for them. It climbs during the TLS handshake
  at boot — 32 in the first minute — and then stops: a thousand sightings of the watched
  beacon later, in the same room, it was still 32. That is one minute of steady state, not
  a night of it, and a flat with a dozen devices in it has not been tried.
- A microphone. The I²S receiver has been run for minutes at a time, with the blocks
  arriving unbroken and the silence measuring as silence, but the three pins have never had
  a part on them. Whether the bit this program samples is the bit a microphone meant, which
  half of the frame a given part drives, and what a real room measures are all unexamined —
  the same gap the 1-Wire bus has, and for the same reason.
- What a loud room does over an evening. The activity rules are tested against synthetic
  tones and the capture against an empty wire, and the two have never met. A threshold
  chosen for a hallway, a fridge compressor, a door slam at three in the morning: none of
  that is anything this board has been asked about.
- I²C, and BME280 with it. Neither the bus nor the part's identification and calibration
  coefficients are here at all, and the hub's catalogue for this platform does not offer
  them.
- Anything that says a converter has something on it. A floating ADC pin reads noise that
  looks exactly like a measurement, and `sensors.cpp` only refuses a count the converter
  could not have produced. The readings above are that noise, honestly labelled and
  honestly meaningless.

## What is left to do

Every limit above is a thing that is not known. This is the shorter list: things that are
known, and that somebody has to go and do. Each one says what it needs, because most of
them are waiting on hardware rather than on a decision.

| What | What it needs | Why it is not done |
|---|---|---|
| The node with a radio, back | A `pico2_w` on a cable long enough to hold BOOTSEL, `build/pico2_w/sentry_firmware.uf2`, and provisioning again | `pico-ingresso` has been off since the vault moved. Record version 2 made everything it kept unreadable, which is the upgrade path working as written — but it still costs a cable and a minute, and nobody has been at the board. |
| A queue that fills, on a board | The same node, and the broker taken away while it is on the air | The wired board hands its readings to the bridge and forgets them. `T14`'s full spool, `link_lost`, the coalescing and the clock-step sweep all want a node that keeps its own queue. |
| A certificate offered *by* the board | The same node, and `forget certificate` followed by one that should be refused | Four certificates have been offered to this broker and answered for, but all of them from a Python client. The rest of `T06` and `T07` is the board's own handshake being refused. |
| The three-hour rule, watched | A node left alone for three hours with nothing telling it the time | `clock=synced` becoming `unknown` on its own is written, tested on host and never seen. It is a long wait rather than a hard one. |
| A sensor with wires on it | A PIR, a reed switch, a DS18B20 with a 4.7 kΩ pull-up, an I²S microphone | `T18` and `T19`. Everything on these paths has been exercised by the board driving its own pad or reading an empty bus. |
| Forty-eight hours of it running | Time, and nothing else | `T40` asks for two days; the longest run here is thirty-five minutes, written up above. |
| A power cut | A supply that can be pulled mid-write | The watchdog resets the chip without taking the power off it, and a `tear` writes half a record on purpose. Neither is the supply sagging during an erase. |
| Two boards at once | A second board | `T39`. One bridge carries a list of them and the code is written for it; there has only ever been one. |
| `PICO-10`, the snapshot | An Arducam SPI module | Declared `not built` in the manifest rather than written and untested. `T32` and `T33` go with it. |
| A threshold that was measured | A house, and nodes in it, over long enough | Two seconds separates a node that is behind from one that is busy, and two seconds is a number somebody chose. The same is true of the two seconds that tell a clock's step from its correction. |
| A node approved again while connected | A decision, then a change in the hub | Watched on 2026-09-17 and left as it is: a session begins at a retained `state`, so a node revoked and re-approved stays offline until it says hello again. |

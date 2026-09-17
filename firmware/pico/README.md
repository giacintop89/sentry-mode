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
| `pico2_w` | RP2350 | yes | 1.00 MB |
| `pico_w` | RP2040 | yes | 1.05 MB |
| `pico2` | RP2350 | no | 275 kB |
| `pico` | RP2040 | no | 294 kB |

The wired ones are a quarter of the size because they compile a different network module —
`net_wired.cpp`, which answers "no radio" and nothing else — and with it no lwIP, no
mbedTLS and no TLS stack at all. Everything else is the same firmware: the sources, the
plan, the vault and the serial console all work on a board that cannot connect to anything,
which is what `PICO-03`'s USB bridge will need. Only `pico2_w` has been run on hardware.

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
# vault at 0x003f6000, 40960 bytes, 10 slots of 4096
#   identity      kept, write 1
#   authority     kept, write 1
#   certificate   kept, write 1
#   key           kept, write 1
#   configuration kept, write 1
```

Names and write counts, never values. The authority would be harmless to print and is not
printed either, because a rule with an exception in it is a rule somebody edits later.

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
| `include/sentry/store.h`, `src/core/store.cpp` | The framing that lets an interrupted write be recognised as one, and the rule for choosing between the two configuration slots. |
| `include/sentry/vault.h`, `src/core/vault.cpp` | Where the five things a node keeps live: two slots each, a whole erase sector apiece, and the most each one may be. |
| `src/device/flash_vault.h`, `src/device/flash_vault.cpp` | The only file that writes to flash: erase, program, read back through the same path the boot takes, and a `tear` that stops halfway on purpose. |
| `include/sentry/identity.h`, `src/core/identity.cpp` | The provisioning record — the node id and where the broker is — and the boot id that must differ every boot. |
| `include/sentry/credentials.h`, `src/core/credentials.cpp` | The authority, the certificate and the key: what each one has to be, taken whole or not at all, and a `forget()` that overwrites the key. |
| `include/sentry/timebase.h`, `src/core/timebase.cpp` | A counter that wraps seen as one that does not, and what a reading may claim about its own timestamp. |
| `tools/emit.cpp`, `tools/unpack.cpp`, `tools/mqtt_emit.cpp`, `tools/client_run.cpp` | Write events, packets, a whole connection and configuration slots for the cross-checks above. |
| `src/device/onewire.h`, `src/device/onewire.cpp` | The 1-Wire bus, bit-banged on one pin: the reset, the slots, and interrupts off for the microseconds that decide what a bit was. The only part of this firmware that cannot be tested off a board. |
| `src/device/main.cpp` | The one program that runs on a board: USB serial, the LED, the die temperature, and the same event writer as everything else. |
| `include/sentry/spool.h`, `src/core/spool.cpp` | The queue for an outage: which readings collapse into a newer one, which are never dropped for them, and what is counted when something is given up. |
| `include/sentry/input.h`, `src/core/input.cpp` | What a wire may mean: the baseline that is not an intrusion, the settling window a PIR needs, the debounce, and the polarity software cannot guess. |
| `include/sentry/sensors.h`, `src/core/sensors.cpp` | Turning what a sensor returned into a reading or into an admission there is none: the 1-Wire CRC, the 85 °C a DS18B20 holds after a reset, and an ADC count nothing could have produced. |
| `include/sentry/plan.h`, `src/core/plan.cpp` | What a configuration is allowed to ask for: which drivers this firmware has, which options each one takes, and a refusal that names the source it failed on — decided whole, on a copy, before a pin is touched. |
| `include/sentry/pins.h`, `src/core/pins.cpp` | Which pins a configuration may use and who already has them, refused whole rather than in part. |
| `tests/` | The nineteen suites, and a tiny harness rather than a test framework. |

Everything under `src/protocol` is pure: no SDK, no clock, no network, no allocation, and
no `malloc` to fail on a board with 264 kB. That is what makes the host build meaningful
rather than a stand-in, and it is the boundary the device half will be built against.

The protocol is compiled with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
-fno-exceptions -fno-rtti`: a change that needs exceptions or runtime type information
fails here, in a second, rather than at link time on a target with neither.

## What is not here, and what it waits on

- Why this board reset, anywhere but on the cable. `status` and the boot line say
  `reset=watchdog` or `reset=power`, and the health message the hub reads says neither:
  there is no field for it in the version 1 contract, and inventing one here would be a
  change to something the Linux agent also writes. The hub can see that a node restarted —
  it counts boot ids — but not what restarted it.
- Keeping the time, as opposed to getting it once. There is a rule now: three of lwIP's
  hourly polls with no answer and the node stops calling what it stamps `synced`, without
  stopping stamping — the offset is still the best estimate it has, and `unknown` is the
  honest word for what it is worth. That rule has never been watched happening. A board up
  for a day, the drift between two answers, and a network that goes away mid-interval are
  all still unexamined, and `T16`'s UTC jump is **not executed**.
- A power cut, as opposed to a reset. Everything above was proved with the watchdog, which
  resets the chip without taking the power off it. What a half-finished flash write does
  when the supply actually sags — the part is mid-erase and the voltage is falling — is not
  something a `tear` verb can stand in for.
- Every driver in the hub's catalogue for this platform is now here: `board`, `gpio`,
  `adc` and `onewire`. A configuration naming anything else — a BME280, a camera, a
  microphone — is refused by name. Camera and microphone commands are
  refused the same way, and will stay refused: this board has neither.
- How long a reading waited. Every event goes out with `queued_ms: 0`, because the queue
  does not record when something was put in it. A reading that waited forty seconds says
  so only through its own `occurred_at`, which is the honest field but not the one the
  hub uses to notice a node that is falling behind.
- The rest of the failure cases of the handshake. A wrong authority has been watched, and
  refused; a certificate issued for another name, an expired one and a revoked one have
  not, so the rest of `T06` and all of `T07` are **not executed**.
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
- I²C, and BME280 with it. Neither the bus nor the part's identification and calibration
  coefficients are here at all, and the hub's catalogue for this platform does not offer
  them.
- Anything that says a converter has something on it. A floating ADC pin reads noise that
  looks exactly like a measurement, and `sensors.cpp` only refuses a count the converter
  could not have produced. The readings above are that noise, honestly labelled and
  honestly meaningless.

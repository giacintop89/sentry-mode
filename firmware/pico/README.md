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
free. The same board has since joined a network and taken its time from it. That is the
first hardware gate of `PICO-01` and no more than that: there is still no MQTT, no flash
and no sensor driver on the board. The rest of the hardware gates in the
[implementation plan](../../docs/sentry-mode-pico-implementation-plan.md) stay marked
**not executed**.

## Building and running the tests

`cmake` 3.20 or newer and a C++17 compiler. Nothing else — no SDK, no network, no board.

```sh
cmake -S firmware/pico -B build/pico-host -G Ninja
cmake --build build/pico-host
ctest --test-dir build/pico-host --output-on-failure
```

Fifteen suites: `json`, `command`, `control`, `event`, `lease`, `topics`, `mqtt`, `session`,
`store`, `identity`, `timebase`, `spool`, `input`, `sensors`, `pins`. The `command` suite
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

That writes `build/pico2w/sentry_firmware.uf2`. Hold BOOTSEL while plugging the board in —
or, if it is running MicroPython, `import machine; machine.bootloader()` — and copy the UF2
onto the `RP2350` volume that appears. The board reboots into the firmware.

`src/device/main.cpp` is one small program and not a satellite. It brings up USB serial and
the wireless chip, names itself from the board's own serial number, makes its boot id from
`pico_rand`, reads the die temperature through `sensors.cpp` and publishes it with the same
`write_event` the host tests exercise. It has no clock of its own, so it publishes nothing
until something tells it the time — a board that stamped readings from the moment it booted
would be writing timestamps nobody measured.

What it listens for on the serial line:

| Line | What it does |
|---|---|
| `provision <json>` | The identity and the network, as `read_provisioning` reads them. It lives in RAM: flash is PICO-02. |
| `join` | Joins that network and starts asking `pool.ntp.org` what time it is. |
| `status` | Address, signal strength, whether the clock is synced, how many answers arrived. |
| `time <unix_ms>` | The time, for a board with no network to ask. |
| `sample` | A reading now, rather than at the next interval. |

The record holds a passphrase, so it is handed over rather than committed:

```sh
firmware/pico/tools/provision_board.py --record .local/pico-provisioning.json --join
```

`.local/` is ignored by git. Nothing prints the passphrase back — not the tool, not the
board — and `identity.cpp` refuses a record with a key for a network it was never given,
because joining the wrong network is worse than refusing to join one.

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

The binary is 327 kB of text and 6.9 kB of static RAM, most of it the wireless firmware and
TinyUSB; `arm-none-eabi-size build/pico2w/sentry_firmware.elf` says so on any change.

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

## What is in here

| Path | What it is |
|---|---|
| `include/sentry/json.h`, `src/protocol/json.cpp` | A reader and a writer for the shapes the contract uses, in buffers the caller owns. No allocation, depth capped at 4, refusing rather than truncating. |
| `include/sentry/event.h`, `src/protocol/event.cpp` | The event envelope, written only after every field has been checked, so a refusal costs nothing and never leaves half an event in the buffer. |
| `include/sentry/command.h`, `src/protocol/command.cpp` | The closed command grammar: the actions, what each one may carry, and who it is addressed to. |
| `include/sentry/control.h`, `src/protocol/control.cpp` | The three control messages this node writes, including the goodbye that has to fit in a 255-byte will. |
| `include/sentry/mqtt.h`, `src/protocol/mqtt.cpp` | The packets, and what this node refuses to speak: no QoS 2, no length it has nowhere to put, no packet only a client may send. |
| `include/sentry/topics.h`, `src/protocol/topics.cpp` | Where each message goes, built in one place because the topic is the one claim a node cannot make up. |
| `include/sentry/names.h`, `src/protocol/names.cpp` | What a name, a uuid, a kind, a driver and a timestamp are, in one place rather than in whichever file needed one first. |
| `include/sentry/session.h`, `src/core/session.cpp` | The order a connection comes up in: nothing is announced before the subscription is confirmed, and every connection has an identifier of its own. |
| `include/sentry/lease.h`, `src/protocol/lease.cpp` | Permission with an end to it, measured on this node's clock, and the memory of the last 64 commands answered. |
| `include/sentry/store.h`, `src/core/store.cpp` | The framing that lets an interrupted write be recognised as one, and the rule for choosing between the two configuration slots. |
| `include/sentry/identity.h`, `src/core/identity.cpp` | The provisioning record — the node id and where the broker is — and the boot id that must differ every boot. |
| `include/sentry/timebase.h`, `src/core/timebase.cpp` | A counter that wraps seen as one that does not, and what a reading may claim about its own timestamp. |
| `tools/emit.cpp`, `tools/unpack.cpp`, `tools/mqtt_emit.cpp` | Write events, packets and configuration slots for the three cross-checks above. |
| `src/device/main.cpp` | The one program that runs on a board: USB serial, the LED, the die temperature, and the same event writer as everything else. |
| `include/sentry/spool.h`, `src/core/spool.cpp` | The queue for an outage: which readings collapse into a newer one, which are never dropped for them, and what is counted when something is given up. |
| `include/sentry/input.h`, `src/core/input.cpp` | What a wire may mean: the baseline that is not an intrusion, the settling window a PIR needs, the debounce, and the polarity software cannot guess. |
| `include/sentry/sensors.h`, `src/core/sensors.cpp` | Turning what a sensor returned into a reading or into an admission there is none: the 1-Wire CRC, the 85 °C a DS18B20 holds after a reset, and an ADC count nothing could have produced. |
| `include/sentry/pins.h`, `src/core/pins.cpp` | Which pins a configuration may use and who already has them, refused whole rather than in part. |
| `tests/` | The fifteen suites, and a tiny harness rather than a test framework. |

Everything under `src/protocol` is pure: no SDK, no clock, no network, no allocation, and
no `malloc` to fail on a board with 264 kB. That is what makes the host build meaningful
rather than a stand-in, and it is the boundary the device half will be built against.

The protocol is compiled with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
-fno-exceptions -fno-rtti`: a change that needs exceptions or runtime type information
fails here, in a second, rather than at link time on a target with neither.

## What is not here, and what it waits on

- Lifecycle and the watchdog. The device program loops and blinks; nothing restarts it
  when it stops, and PICO-01 asks for a watchdog that has been seen to fire.
- The flash itself: the linker layout, the erase and program calls, USB recovery, and the
  certificates and private key a real provisioning tool writes. `store.cpp` says what a
  record looks like and `pack_provisioning.py` writes one; neither has touched a sector.
- Keeping the time, as opposed to getting it once. The board asks at boot and then every
  hour, and a board that has been up for a day has not been watched: what the drift between
  two answers is, what happens when the network goes away mid-interval, and whether a node
  that has lost its clock should go back to `unsynced` rather than keep stamping readings
  from a counter nobody has checked.
- The MQTT client itself. `mqtt.cpp` writes and reads the packets and is checked against a
  second implementation, `session.cpp` says what order things happen in and `lease.cpp` says
  what permission is worth — but nothing yet holds a socket, retries, tracks what is in
  flight or does the mTLS handshake. No board has connected to the broker, so the
  compatibility run against a live one that PICO-03 asks for has **not** been done.
- Any sensor driver at all. What is here is the part of one that has no hardware in it:
  `input.cpp` decides what a level means, `sensors.cpp` decides what a scratchpad or an
  ADC count means, and `pins.cpp` decides whether a configuration may start. Nothing has
  read a pin, waited on a 1-Wire bus or started a conversion. BME280, with its
  identification and its calibration coefficients, is not here at all.

# Pico satellite firmware

A satellite that is a microcontroller rather than a Linux board. This directory holds the
firmware for it, and today it holds exactly the part of that firmware which can be built
and run without one: the protocol — JSON, the event envelope, the command grammar and the
lease — and the core it will sit on — the two configuration slots, the identity and the
timebase. All of it compiled for the host and tested against the same contracts and
fixtures the hub and the Pi Zero agent are tested against.

**Nothing here has run on an RP2040 or an RP2350.** There is no SDK pinned, no Wi-Fi, no
MQTT, no sensor, no UF2. `PICO-01` of the [implementation
plan](../../docs/sentry-mode-pico-implementation-plan.md) also asks for a board target, a
heartbeat and a proven watchdog, and `PICO-02` for flash, USB recovery and SNTP; those need
hardware nobody has wired up yet, so, as §13.1 of the plan requires, what is delivered is
the build, the host tests and a repeatable procedure, and the hardware gates are marked
**not executed**.

## Building and running the tests

`cmake` 3.20 or newer and a C++17 compiler. Nothing else — no SDK, no network, no board.

```sh
cmake -S firmware/pico -B build/pico-host -G Ninja
cmake --build build/pico-host
ctest --test-dir build/pico-host --output-on-failure
```

Seven suites: `json`, `command`, `event`, `lease`, `store`, `identity`, `timebase`. The
`command` suite reads the fixtures in
`contracts/satellite/v1/control/fixtures/`, the same files `tests/unit/test_satellite_control.py`
and `satellite/tests/unit/test_control_contracts.py` read, so a fixture the hub accepts and
this firmware refuses is a failure here rather than a surprise on a board.

`make pico` runs all three steps and the cross-check below in one go.

Then the cross-check, which is the one that matters most:

```sh
.venv/bin/python firmware/pico/tools/check_against_contracts.py --build-dir build/pico-host
```

It runs the firmware's own serializer (`sentry_emit`) and hands every event it writes to
the hub's Pydantic models and to the satellite's schema checker. A serializer validated
against the schema it was written from proves very little; one validated by two
implementations that have never seen it proves something. It has already earned its place:
it caught this firmware calling a reading `stale` when the hub's vocabulary says
`degraded`, which every test in this directory was happy to accept.

The same idea applies to the flash framing:

```sh
.venv/bin/python firmware/pico/tools/pack_provisioning.py --build-dir build/pico-host --verify
```

That writes configuration slots from the header layout rather than from the C++, and asks
the firmware (`sentry_unpack`) what it read back — including from a record whose last three
bytes never made it, which must leave the previous configuration in charge.

## What is in here

| Path | What it is |
|---|---|
| `include/sentry/json.h`, `src/protocol/json.cpp` | A reader and a writer for the shapes the contract uses, in buffers the caller owns. No allocation, depth capped at 4, refusing rather than truncating. |
| `include/sentry/event.h`, `src/protocol/event.cpp` | The event envelope, written only after every field has been checked, so a refusal costs nothing and never leaves half an event in the buffer. |
| `include/sentry/command.h`, `src/protocol/command.cpp` | The closed command grammar: the actions, what each one may carry, and who it is addressed to. |
| `include/sentry/lease.h`, `src/protocol/lease.cpp` | Permission with an end to it, measured on this node's clock, and the memory of the last 64 commands answered. |
| `include/sentry/store.h`, `src/core/store.cpp` | The framing that lets an interrupted write be recognised as one, and the rule for choosing between the two configuration slots. |
| `include/sentry/identity.h`, `src/core/identity.cpp` | The provisioning record — the node id and where the broker is — and the boot id that must differ every boot. |
| `include/sentry/timebase.h`, `src/core/timebase.cpp` | A counter that wraps seen as one that does not, and what a reading may claim about its own timestamp. |
| `tools/emit.cpp`, `tools/unpack.cpp` | Write events, and read configuration slots, for the two cross-checks above. |
| `tests/` | The seven suites, and a tiny harness rather than a test framework. |

Everything under `src/protocol` is pure: no SDK, no clock, no network, no allocation, and
no `malloc` to fail on a board with 264 kB. That is what makes the host build meaningful
rather than a stand-in, and it is the boundary the device half will be built against.

The protocol is compiled with `-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror
-fno-exceptions -fno-rtti`: a change that needs exceptions or runtime type information
fails here, in a second, rather than at link time on a target with neither.

## What is not here, and what it waits on

- The device build. It needs a pinned Pico SDK, a pinned lwIP and Mbed TLS, and a first
  build on a real board. Offering `-DPICO_BOARD=pico_w` today would be offering a build
  that has never produced a binary.
- Lifecycle, watchdog and the USB/LED heartbeat — PICO-01's hardware half.
- The flash itself: the linker layout, the erase and program calls, USB recovery, and the
  certificates and private key a real provisioning tool writes. `store.cpp` says what a
  record looks like and `pack_provisioning.py` writes one; neither has touched a sector.
- SNTP. `Timebase` is told the time by something; nothing here is that something yet.
- MQTT and TLS — PICO-03.
- Any sensor driver at all.

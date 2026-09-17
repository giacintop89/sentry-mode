# Presence

**Status:** built; the scanner was run against real BlueZ on a Zero W and on the hub, with a real advertisement seen
**Gate:** the PR-12 gate of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)
**Decision:** presence is one known device, seen by a satellite over Bluetooth LE through BlueZ on D-Bus, with a D-Bus client of our own. Presence never disarms anything, and Wi-Fi presence stays a shape, not a supported feature.

## What presence is here

One device per source, named in the node's own configuration: an address, or an iBeacon
UUID for a beacon that rotates its address. The node answers one question about it —
`present`, `absent` or `unknown` — and sends nothing else. No address, no signal strength,
no inventory of what else is in the air, nothing in the general logs. A neighbour's phone
passing the window is never mentioned, because nothing but the configured device is ever
matched, and an advertisement that does not match is dropped before it is even decoded.

## Why BlueZ on D-Bus, and why our own client

`bluetoothctl` is an interactive program whose output is meant for a person; parsing it
means depending on wording and on a terminal. BlueZ's own interface is D-Bus, where
discovery is a filtered `SetDiscoveryFilter` + `StartDiscovery`, devices arrive as
`InterfacesAdded` and each new advertisement as a `PropertiesChanged`. That is the
interface this uses.

The packaged bindings did not fit the board. `python3-dbus-next` on Debian pulls in
`python3-gi`, and `python3-dbus` needs a GLib main loop running in the process — both of
them a large dependency on a Zero W for what is, in the end, a socket, a fixed
handshake and a few marshalled messages. `presence/dbus.py` is that socket: EXTERNAL
authentication, `Hello`, method calls with replies, `AddMatch`, and a signal reader. It is
tested against a socket that speaks the protocol back, so the marshalling is exercised for
real, and it was run against the system bus on both machines.

It is deliberately small. It speaks little-endian only, refuses a signature it does not
know, and is not a general D-Bus library; it is what BlueZ needs and nothing more.

## Cost on the Zero W

A 30-second LE scan in a normal flat saw 14 devices and about 660 advertisements, and
decoding all of them cost around 3.1 s of CPU — roughly a tenth of the core, for data that
was thrown away. The reader therefore looks at the raw bytes of a signal first and only
decodes the ones whose object path or interface can possibly matter. The skipped ones are
counted, so the health report can show that the filter is doing its job rather than hiding
a bug.

Scanning shares the same radio as Wi-Fi on a Zero W, so it is allowed in
`sensor-presence` only. A board that streams video or sound refuses a `ble` source: that
combination has not been qualified.

## What makes a presence

Three defaults, all of them configurable, and all of them about not guessing:

- **A sighting is an advertisement received now.** BlueZ remembers devices it has seen
  before and hands them over on connect; those are recorded but never counted as sightings.
- **Sightings have to be spread out.** Advertisements arrive in bursts; sightings closer
  together than a second count once, so `enter_sightings = 3` within ten seconds means the
  device really was there for a while.
- **Absence needs coverage.** Absence is declared only after `absent_after_seconds` of
  scanning that actually ran. A stopped scan, a blocked adapter, a `bluetoothd` restart or
  a lost hub connection produce `unknown`, and the clock for absence starts again when
  scanning does.

RSSI is used only as a floor (`rssi_min`), to keep the device in the room rather than in
the street. It is not converted into a distance: that needs a calibration design of its
own, which this is not.

## Several nodes watching one device

A rule may name up to eight sources for one device. The hub combines them the only way
that is honest: **present** as soon as one says so, **absent** only when they all do,
**unknown** otherwise. A rule fires on a change into the state it asks for, so a device
that moves from the hall to the garage never leaves, and a node repeating itself is not an
arrival. Every rule starts at unknown, so arming does not fire on the state of the world.

## Presence is not authentication

Nothing here disarms Sentry, unlocks anything, or stands in for a person. A BLE
advertisement can be watched, copied and replayed by anyone within range, and the device
is a tag, not its owner. A rule may greet, light, record or message on a presence; the
hub has no action that would let it do more.

## Wi-Fi: a shape, not a feature

`src/sentry_mode/presence/wifi.py` defines what a router would have to provide — a
client table with a real association flag and a time it was observed — and what the hub
may conclude from it: an association makes a device present, a DHCP lease never does, and
an answer older than a minute makes it unknown. The only provider is one that reads a
snapshot from a file, which is enough to exercise the rules.

No router is supported, and none is claimed to be. Supporting one means a read-only
service identity on a named firmware, a pinned response shape, a timeout and a rate limit,
proven against the real box; until that exists, presence is BLE. Scanning access points
from a satellite answers a different question entirely and is not this.

## Not done

- No hardware gate with a real tag over hours: the scanner was exercised against real
  BlueZ, but the three-sightings and two-minute defaults have not been lived with.
- No adapter reset from inside the node. A wedged adapter is reported as `unknown` with
  its reason, and `rfkill` or a restart is a person's job.
- No second adapter on one node, and no distance, direction or zone from signal strength.

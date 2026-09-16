# Sensor drivers on the board

**Status:** implemented and tested against fakes; GPIO adapter exercised against libgpiod 2.2.1 on the hub board — 17 September 2026
**Gate:** PR-06 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md); G1 still needs a real PIR on a Zero W

A satellite now drives four kinds of source besides the simulated one: a digital input
(a PIR or a door contact), a DS18B20 probe, a BME280 and an ADS1115 converter. This page
records how each one talks to the hardware, what it reports when that fails, and how the
hub may change the list of sources on a running node.

## One adapter per bus, and nothing else touches hardware

`sensors/gpio.py` and `sensors/i2c.py` are the only modules that open a device. Drivers
get a factory, so the tests hand them a scripted line or bus and the agent itself imports
neither. `drivers.Hardware` holds the factories and shares one bus handle, and one BME280,
between the sources that use the same address.

### GPIO: libgpiod 2, from the distribution

Raspberry Pi OS Trixie ships `python3-libgpiod` 2.2. It is the character-device interface
the kernel supports, it is packaged for ARMv6, and it needs no build on the board. The
sysfs interface and the libraries built on it are deprecated, and a pip wheel would have
to be compiled on the Zero. `gpiod` is imported only when a line is opened, so a board
without it still validates, runs its other sources, and says what to install.

Lines are named in BCM numbers (`line_numbering = "bcm"` is required, not assumed). A line
is requested as an input with edge detection on both edges and the bias the file names.
Debouncing is done in software: the driver waits `debounce_ms` after the first edge,
drains what came in, reads the level, and reports only if the state actually changed.
Kernel debouncing is not available on every chip and would hide the timing from the tests.

`read_edge_events()` blocks when nothing is queued. The adapter only reads while
`wait_edge_events(0)` says something is there. A driver hung on this call was the first
bug the real library found, and a test now fakes a module with the same behaviour.

A line already held by another program is refused with that program's name. A missing
chip or an offset outside the chip are refused with what was wrong.

### Settling is not an edge

A PIR spends its first seconds, or minutes, finding its own baseline. `settle_seconds`
waits that long, throws away every edge from that time, and then sends the level as a
baseline (`initial_state`), which the hub records and never takes as news. There is no
universal default: the value belongs in the node file, measured on the module that is
actually wired up.

### 1-Wire: the kernel's driver

`dtoverlay=w1-gpio` makes each DS18B20 a file. The driver reads it and refuses anything
the kernel did not confirm: a CRC that is not `YES`, no `t=`, a value outside −55 to
125 °C, and 85 °C exactly — the power-on value a probe returns when it lost power between
conversions. The probe id is checked against `28-` and twelve hex digits before any path
is built from it.

### I2C: `/dev/i2c-N`, no library

A register read or write is an `ioctl(I2C_SLAVE)` and a `read`/`write` on the device file.
That is a dozen lines, where `smbus2` would be a dependency. Each address has a lock, so
two sources on one part never interleave a transaction.

The **BME280** runs in forced mode: one measurement per request, then the part sleeps, so
it does not warm itself and read its own heat as the room's. The three quantities are
three sources sharing one part, which measures at most once a second and hands each its
share. Compensation is Bosch's floating-point version; the unit test checks it against the
worked example in the datasheet. The chip id is checked, and a bus error drops the stored
calibration in case the part was swapped.

An **LDR** cannot be read on a Raspberry Pi GPIO, which has no analogue input. It needs an
ADS1115, and the driver reports what the circuit gives: `ratio` (a fraction of the
reference voltage) or `volts`. Neither is lux, and the unit says so. A value clearly
outside the divider's range is refused rather than clamped into a plausible one.

## What a failed read is

Every periodic read runs on a helper thread with a timeout. A bus that hangs costs one
reading, and a read still running when the next one is due is refused as busy rather
than piling up threads. A failure is reported once as `unavailable` with no value, and
again only after a good reading in between. It is never a zero, which a threshold rule
would happily believe.

## Changing the sources from the hub

The hub may send `configure` with a revision number and a list of sources. That is the
whole of it: the command has no field for the network, the certificates, the hub address
or anything installed.

1. The list is checked with the same rules as the file, narrowed to the kinds this agent
   has a driver for, including pin, bus and probe conflicts. The revision has to be newer
   than the one running.
2. The drivers are built with throwaway hardware, which opens nothing. Anything refused so
   far changes nothing.
3. The old drivers stop and release their lines, the new ones start, and for two seconds
   each is watched: a driver that dies, or reports an error such as a line held by
   another program, puts the previous list back.
4. The accepted list is written to `/var/lib/sentry-satellite/sources.json`, atomically
   and readable only by the agent. If that fails, the previous list goes back too, so a
   restart never brings back something the hub was told had not been applied.
5. The node publishes its state again with `config_revision`. The hub keeps the session
   and its grant, updates the sources, and marks the ones no longer declared as disabled.

The installed file under `/etc` is never rewritten. At start the overlay replaces its
sources if it still passes the checks; if it does not — an upgrade dropped a driver, say —
it is set aside with a warning and the installed list runs.

The node answers `received` at once, then `applied` or `failed` with the reason. The work
happens off the network thread. A second request while one is being applied is refused
without being remembered, so it can simply be sent again; a repeated command id gets the
answer the first one got.

## Not yet verified

- A PIR, a DS18B20, a BME280 and an ADS1115 on a Zero W. The drivers are tested against
  scripted lines and buses; the GPIO adapter was run against libgpiod on the Pi 5 with
  nothing wired. G1 is the gate for the PIR.
- The debounce and settle values for the module that will be installed.
- `/dev/i2c-1` on the Zero, which needs `dtparam=i2c_arm=on`.

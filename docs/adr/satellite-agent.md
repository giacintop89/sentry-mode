# What runs on the board

**Status:** measured — 16 September 2026
**Board:** Raspberry Pi Zero W Rev 1.1, `armv6l`, Raspberry Pi OS Trixie, Python 3.13.5
**Gate:** PR-02 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)

The agent is a peripheral of the protocol, not a second Sentry. It reads what is wired to
it, says so, does what a signed command allows, and stops when asked. It holds no rules,
makes no decisions about the house, and cannot be told to run a program.

## A separate distribution, and why

`satellite/` is its own Python project with its own `pyproject.toml`. It shares no code
with the hub — a test walks every module and fails on an import that is neither the
standard library, Paho, nor the agent itself, and another asserts the string `sentry_mode`
appears nowhere in it. What the two sides share is `contracts/`, which both are tested
against and neither owns.

That is not tidiness. Every dependency is a package that has to exist for ARMv6, build
there, and be maintained by somebody; the board has one core and 302 MiB free.

## Measured on the real Zero W

The wheel was built on the Pi 5, copied over, and unpacked with `zipfile` — the board has
no `pip`, and did not need one.

| Check | Result |
|---|---|
| `doctor` without Paho, CA or identity | exits 1, naming all three; everything else still reported |
| `identity --create` | wrote `identity.json` at mode 600 |
| `validate` | accepted the simulated profile |
| `run`, then SIGTERM | stopped in 8 ms, exit 0 |
| Resident memory while running | 15.5 MiB |
| Threads | 4: the main thread, the network, the health beat, one driver |
| Board at the time | 36.9 °C, `throttled 0x0`, clock synced, 302 MiB available |

`ffmpeg` is not installed on this board; `rpicam-vid`, `arecord` and `vcgencmd` are. That
is a fact for PR-08, not a problem now.

## Decisions this increment fixes

**The identity is issued once.** `identity --create` refuses to overwrite an identity that
exists. Re-issuing one would orphan every rule the hub holds against the old name, so it
is an administrative act with a new certificate, not something a first boot can do by
accident. The **boot id is the opposite**: a fresh one every run, so the hub can tell a
restart from a reconnection and never compares sequence numbers across two lives of the
same process.

**Nothing is published before the hub allows it.** The agent connects, says what it is on
a retained `state` topic, and beats on `health`. Events accumulate in the spool and go
nowhere until a grant arrives, and stop again the moment it expires. A node that comes
back after an outage therefore cannot flood a hub that has not yet decided it should.

**The queue is bounded three ways** — how many, how big, how old — and the oldest is
dropped first, counted by reason, and reported in health. An unbounded queue on a board
with no useful swap loses everything at once instead of losing the oldest thing.

**Commands are a closed list**: grant, renew, revoke, stop. There is no command that names
a program, a file or a rule. A command addressed to another node is refused, a repeat is
answered with the first answer rather than acted on again, and a renewal that is not newer
than the grant it renews is refused. A flaky link produces all three of those on its own,
without anybody meaning any harm.

**A driver that hangs costs its own source.** Each driver reads on its own thread and is a
daemon, so the network and the health beat are untouched and the process still leaves.
What the agent will not do is pretend: `stop()` names the threads that did not stop and
the exit status says so, because silence would make an unstoppable sensor look like a
clean shutdown.

## One deviation from the plan, on purpose

Section 6.4 of the implementation plan shows `source_id` on the wire as
`zero-ingresso.pir-1`. The agent sends the local half only — `pir-1` — and the hub joins
it to the node identity it authenticated from the topic. A satellite then cannot name a
source belonging to another node even by lying, and there is no second spelling of the
same thing to keep in step. Configuration files may still write a source out in full, and
the agent checks the node half is its own before dropping it. See
[naming a source](satellite-identity.md).

## Two schemas, one contract

The hub validates with Pydantic; the satellite, which will never have Pydantic, validates
against the published JSON schema. Both run against the same fixtures, so "the hub accepts
it" and "the satellite accepts it" cannot quietly diverge. Two rules Pydantic knows but a
schema only advises — that a UUID is a UUID, and that a timestamp carries an offset — are
written into the schema as patterns, because `format` binds nobody. The satellite's
checker refuses any schema keyword it does not understand rather than passing it.

## What is still pretend

Every driver except `dummy` raises at `validate` and says which increment brings it: GPIO
and the other sensors with PR-06, the camera with PR-08, the microphone with PR-11,
Bluetooth presence with PR-12. `scripts/satellite_simulator.py` produces the traffic
instead — seven scenarios, reproducible from a seed, including the node whose clock is a
year out, the node that floods after twenty minutes off the air, and the sensor that
degrades and then stops answering. Every scenario is checked against the published schema,
and a simulated event contains a reading and nothing else.

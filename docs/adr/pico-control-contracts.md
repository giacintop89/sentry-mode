# Control contracts before the Pico firmware

**Status:** written and held to by three implementations — the hub, the Linux agent and, since PICO-01, the firmware's own C++ (`firmware/pico`, host build only); **nothing here has been compiled for or run on an RP2040 or an RP2350**
**Gate:** the first half of PICO-00 of the [Pico implementation plan](../sentry-mode-pico-implementation-plan.md)
**Decision:** write down `state`, `health`, `commands` and `acks` as generated schemas with shared fixtures, before any C++ is written, and change nothing on the wire while doing it.

## Why this comes first

The event envelope has had a contract since the first satellite: a schema generated from
the models, fixtures both sides are held to, and a test that fails when they drift. The
four control channels never did. One side wrote a dictionary, the other read it with
`.get()` and a default, and the agreement lived in two files that happened to be written
in the same week by the same people.

That is affordable between two Python programs. It is not affordable when the second
implementation is C++ on a microcontroller: a field that is sometimes absent, a number
that is sometimes a float, and a string of no stated length are each free in Python and
each an unbounded buffer there. None of them can be discovered by reading a `.get()` call,
and all of them would be discovered later, on a board, by way of a crash.

So the contract is written from what is already being said, not from what would be nicer
to say. Nothing on the wire changed.

## What was decided

**The hub's readers stay tolerant.** `SatelliteService` still reads a heartbeat with
`.get()`. A contract says what a sender may rely on being accepted; turning it into a new
way to throw away a heartbeat would be a regression dressed as rigour.

**Three channels are closed, one is open.** `state`, `command` and `ack` refuse fields
nobody agreed on. `health` keeps them: a board reports what it can measure, and a
microcontroller has a free-heap figure and a reset reason that no Linux node will ever
send, while having no load average at all. What a board cannot measure is absent or
`null` — never zero, which is a measurement.

**A command carries no `schema_version`, on purpose.** Agents refuse fields they do not
know, so adding one would mean every hub rejecting every node it was talking to a minute
earlier. Commands are versioned by the contract directory, like the topics. This is the
one asymmetry in the four, and it is written down rather than tidied away.

**A `state` is optional after `online`.** The same message is registered with the broker
as the node's will, and the lwIP MQTT client writes a will's topic and payload with one
length byte each. A compact goodbye — who, which boot, which connection, gone — is 165
bytes for the node name in the fixture, and the test that measures it is in the suite. The
full snapshot is that same message with the rest filled in, and the hub reads exactly the
fields the compact one carries before it closes a session.

**Limits are in the contract, not in a comment.** At most 32 sources, option values of at
most 128 characters, identifiers of at most 64, a detail of at most 256, a stream of at
most 600 seconds to an unprivileged port. These are the numbers the two implementations
already used; writing them into the schema is what lets a third one size a buffer without
reading either.

**A command's grammar is checked by the parser, not by the schema.** Which fields a
command may carry depends on what it is asking for: a `stop` that carries a ticket is not
a stop with something extra. A schema cannot state that without becoming an interpreter
nobody wants on a microcontroller, so the invalid command fixtures are given to the
agent's own parser and to the hub's models, and the two must agree.

## What this is checked against

- The 10 valid and 10 invalid fixtures, by the hub's Pydantic models, by the agent's
  schema checker and command parser, and by the firmware's command parser: same files,
  same verdicts.
- What the firmware itself writes: `firmware/pico/tools/check_against_contracts.py` runs
  its serializers and hands every event, every state, every heartbeat and every answer to
  the hub's models and to the agent's schema checker, and has the hub's own topic parser
  read back the topics it would publish on. This is how the firmware was caught calling a
  reading `stale` where the vocabulary says `degraded` — a drift no test written on the
  C++ side could see.
- What the Linux agent really publishes: its snapshot, its will, its heartbeat and its
  answers to commands are validated against the schemas in the agent's own suite.
- What this hub really sends: the grant, the configuration and the revocation a session
  produces are taken out of the transport and validated as commands.
- What is on the wire right now: 12 messages captured from the live broker, from a
  Raspberry Pi Zero W and a simulated node, all accepted. This is a check, not a
  qualification: it says the contract describes today's traffic.

## What is not checked, and must not be claimed

No firmware was built. Nothing here says an RP2040 can hold a TLS session and an MQTT
client at once, that the SDK's lwIP has the same will limits as upstream, or that Mbed TLS
verifies the broker's name in the configuration we would ship. Those are the rest of
PICO-00 and they need a board, a pinned SDK and a measurement — not a schema.

The satellite's schema checker grew two keywords (`maxItems`, and
`additionalProperties` as a shape rather than `false`) to check these files. It still
refuses a schema with a keyword it does not implement, which is what keeps it from
passing documents it never really examined.

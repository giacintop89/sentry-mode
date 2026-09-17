# Control messages, version 1

Everything a node and the hub say to each other that is not an event: what a node is
(`state`), how it is getting on (`health`), what the hub asks it to do (`command`), and
what it answers (`ack`). The event envelope next door has had a contract since the first
satellite; these four had one side writing and the other side reading with `.get()`.

That was survivable while both ends were Python written in the same week. It stops being
survivable when the other end is a microcontroller with fixed buffers, so what was already
being said is written down here, with the limits an implementation needs to size a buffer
for it. The schemas are generated from `src/sentry_mode/satellites/control.py` by
`scripts/generate_contracts.py`; do not edit them by hand.

| Topic | Message | Who sends it | Retained |
| --- | --- | --- | --- |
| `<prefix>/nodes/<node>/state` | `state.schema.json` | the node, and the broker as its will | yes |
| `<prefix>/nodes/<node>/health` | `health.schema.json` | the node, every heartbeat | no |
| `<prefix>/nodes/<node>/commands` | `command.schema.json` | the hub | no |
| `<prefix>/nodes/<node>/acks` | `ack.schema.json` | the node, once per command | no |

## What is closed, and what is open

`state`, `command` and `ack` refuse fields nobody agreed on: a sender that misspells one
is told, rather than left believing it said something that was quietly dropped.

`health` is open, and deliberately. It is diagnostics, and a board reports what it can
measure: a microcontroller has no load average, and it has a free-heap figure and a reset
reason that no Linux node will ever send. A hub that threw away a whole heartbeat because
one counter was new would lose the evidence exactly when a new kind of node arrives. What
a board cannot measure is absent or `null`, which is not the same as zero and must never
be written as zero.

## Two things the wire does that look like mistakes

- **A command carries no `schema_version`.** Nodes refuse fields they do not know, so
  adding one would mean every hub rejecting every node it was talking to a minute earlier.
  Commands are versioned by the directory they are published in, like the topics.
- **A `state` is almost entirely optional after `online`.** It has to be: the same message
  is registered with the broker as the node's will, and the lwIP MQTT client writes a
  will's topic and payload with a single length byte each. A goodbye says who is leaving,
  which boot and which connection is ending, and that it is gone — and fits in 255 bytes.
  The full snapshot is the same message with the rest filled in.

## Fixtures

`fixtures/valid/` holds messages both sides must accept and `fixtures/invalid/` messages
both sides must refuse, named by the channel they belong to. The hub checks them with
Pydantic; the agent checks them against the schema, and checks commands with the parser
that decides what it will actually act on, because which fields a command may carry
depends on what it is asking for — a rule a schema cannot state and the two sides still
have to agree on.

| Fixture | Why it is refused |
| --- | --- |
| `state-unknown-field.json` | A field nobody agreed on, on a channel that is closed. |
| `state-source-id-not-a-name.json` | A source is named on its own node. Joining it to the node is the hub's job, and a node must not be able to claim a source on another one. |
| `state-option-longer-than-the-limit.json` | An option is bounded, because the other end has to decide in advance how much room to keep for it. |
| `health-a-queue-that-counts-backwards.json` | Health is open to fields nobody knows yet, not to numbers that cannot happen. |
| `command-action-nobody-agreed-on.json` | The list of actions is short and closed. Anything else is not a command with a new feature, it is not a command. |
| `command-grant-without-a-capability.json` | Lending something means saying what is being lent. |
| `command-configure-numbered-from-zero.json` | A configuration is numbered from 1, so that "none yet" and "the first one" cannot be confused. |
| `command-stop-carrying-a-token.json` | Stopping a stream needs only its id. A stop that carries a ticket is a message nobody meant to send. |
| `command-stream-to-a-privileged-port.json` | The hub's media port is an unprivileged one, and a node will not be talked into opening anything else. |
| `ack-outcome-not-in-the-vocabulary.json` | An outcome is one of three words, because the hub branches on it. |

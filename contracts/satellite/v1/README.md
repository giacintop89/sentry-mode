# Satellite contract, version 1

The hub and the satellites are two separate programs that have to agree on what a message
means. This directory is that agreement, and it is the only place where it is written down.

- `event.schema.json` is generated from `src/sentry_mode/satellites/protocol.py` by
  `scripts/generate_contracts.py`. Do not edit it by hand: change the models and
  regenerate. A test fails if the committed file and the models have drifted apart.
- `audio.json` is the layout of the blocks of sound a microphone sends to the hub's media
  port, generated from `src/sentry_mode/audio/blocks.py` by the same script. The agent's
  tests pack a block and check it against this file.
- `fixtures/valid/` holds messages both sides must accept, `fixtures/invalid/` messages
  both sides must reject. The hub checks them against its Pydantic models; the satellite,
  which has no Pydantic, checks them against the schema. Same files, same verdicts.

A rejected message is rejected for one reason, named by the file:

| Fixture | Why it is refused |
| --- | --- |
| `unknown-field.json` | A field nobody agreed on. Unknown fields are refused rather than dropped, so that a sender never believes it said something the hub ignored. |
| `source-id-not-a-name.json` | `source_id` is the name a source has on its own node. Joining it to the node is the hub's job, and a satellite must not be able to claim a source on another node. |
| `kind-without-family.json` | An event kind is a family and a name, as in `sensor.motion`. |
| `timestamp-without-zone.json` | A reading whose time zone is a guess cannot be ordered against anything else. |
| `sequence-negative.json` | Sequence numbers only ever count up, within one boot. |
| `schema-version-from-the-future.json` | Version 1 accepts version 1. A later version is a different contract, not a longer one. |
| `quality-not-in-the-vocabulary.json` | Quality is a closed list, because rules branch on it. |
| `event-id-not-a-uuid.json` | Identifiers have to be unique across nodes that have never met. |

Version 2 will be a new directory next to this one, not an edit of this one.

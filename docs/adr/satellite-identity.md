# How a source is named, and who is allowed to say so

**Status:** decided — 16 September 2026
**Gate:** PR-01 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)

Once there is more than one camera, every rule, every log line and every stored event has
to say which one it means, forever. A name that changes is a name that breaks a rule
someone wrote months ago, so the grammar is fixed here before anything starts using it.

## The grammar

A name is 1 to 40 characters of lowercase ASCII letters, digits and hyphens, starting and
ending with a letter or a digit: `^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$`.

| Kind of source | Identifier | Example |
|---|---|---|
| On this node | the name alone | `legacy-primary` |
| On a satellite | the node, a dot, the name | `zero-entrance.pir-1` |

The dot is the only separator, which is why it cannot appear inside a name. Zones use the
same grammar (`entrance`, `garage`). Display names are free text in any language
("Ingresso, sopra il cancello"); they are what a person reads, never what a rule matches.

English and ASCII are not a preference about the house. These strings end up in MQTT
topics, file names, SQLite keys and log lines on four different machines, and every one of
those places has its own opinion about anything else.

## Immutability

`SourceRef` and `SourceRecord` are frozen: an identifier cannot be edited after it is
issued. Renaming a thing is a display change. Moving a sensor to another node makes a new
source, because a rule that fires on `zero-entrance.pir-1` must never silently start
firing on a sensor in the garage. What does change is `state`, through the registry,
which replaces the record and leaves the reference alone.

## Who joins the two halves

A satellite sends `node_id` and `source_id` separately, and `source_id` is only the name
the source has on that node. The hub joins them. A satellite therefore cannot claim a
source belonging to another node, even by lying, because the node half of the identifier
is the one the broker authenticated. `fixtures/invalid/source-id-not-a-name.json` is that
rule, written as a message both sides must refuse.

## One authority per fact

The wire format lives in `src/sentry_mode/satellites/protocol.py`. The JSON schema the
satellites validate against is generated from it by `scripts/generate_contracts.py`, and
a test fails if the committed file and the models disagree. The schema is never edited by
hand, so there is no second definition to keep in step.

Two rules cannot be expressed by a schema's `format` keyword alone and are written out as
patterns instead: names, and timestamps that carry an offset. A reading whose time zone is
a guess cannot be ordered against anything else, so `2026-09-16T21:04:07` is refused while
`2026-09-16T21:04:07Z` is accepted.

## The devices that were already here

The camera, microphone and speaker on the Pi 5 are registered as `legacy-primary`,
`legacy-microphone` and `legacy-speaker`. Nothing about them changes: the registry records
the configured `device` exactly as it was written, integer index or path or URL, and the
tests assert the type as well as the value. The names exist so that a rule written today
still means the same camera once there are three.

## What this costs the node today

Nothing observable. `satellites.enabled` is `false`, no optional dependency is imported,
and the only change to the read-only API is one new section in `/api/config`, which the
baseline fixture was re-recorded for. The identity of a source is available inside the
process; nothing yet asks for it.

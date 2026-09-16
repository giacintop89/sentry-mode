# What it takes to be believed

**Status:** decided — 17 September 2026
**Gate:** PR-03 of the [implementation plan](../sentry-mode-zero-w-implementation-plan.md)

A satellite is a small computer in a hallway. It will be updated rarely, it can be
unscrewed from a wall and carried away, and whatever is on it is on it. Everything here
assumes that one day one of them will be someone else's.

## Four things have to agree

| Question | Answered by |
|---|---|
| Is this connection allowed at all? | A certificate from our own authority. The broker refuses anonymous connections and requires one. |
| What is it allowed to be called? | The name in that certificate, which the broker uses as the username. |
| Where may it write? | An access list generated from the registry: its own topics, nobody else's. |
| Is it this exact node? | The fingerprint the record was approved with. |

The hub learns who sent a message from the **topic**, not from the message. That works
only because the broker will not let a node write anywhere but under its own name; without
that access control a `node_id` in a payload is a claim like any other, and the hub checks
the two against each other anyway.

A valid signature is not permission. A certificate this authority issued, for a name that
was never registered, gets a node as far as the broker and no further.

## Registered, then approved

Two steps, deliberately. A registered node is known to exist; an approved node is listened
to, and approval pins the record to the certificate in hand. Another certificate from the
same authority, for the same name, is refused — the record is tied to the bytes, because a
name can be reissued and a serial can be reused by a careless authority.

Revocation keeps the record. History is not a permission, and deleting the node would take
the reason it was removed with it.

## Revocation does not wait for the broker

Reloading a broker's configuration does not close connections that are already open. So
the hub takes the permission away on its own side first: the session ends, the grants end,
the retained snapshot is erased, the node's sensors are marked unavailable, and anything
arriving afterwards is refused whether or not the socket is still up. The ACL and the
reload are the second half, and the tool says so rather than letting anyone assume the
first half was enough.

## Epochs, grants and old news

Every connection gets a number that has never been used before. The counter is persisted
and only ever counts up, across restarts and across crashes, so a grant from a previous
life of the hub cannot be replayed into this one and still look current.

Nothing is published before the hub grants it, and a grant belongs to one connection:
after a reconnection the old one is worthless. That is what stops a node returning from an
outage from flooding the hub with news from an hour ago, and it is checked on the hub
rather than trusted to the `replayed` flag the sender set.

The same reasoning covers a Last Will that took the long way round. The will names the
connection it belongs to, and a goodbye about a connection that is already over is counted
and dropped instead of burying a node that is back.

## The link and the nodes fail apart

A broker that has gone is one fault, reported as `transport_unavailable`. Reporting it as
fifteen nodes that all went offline at the same instant would be true in the narrowest
sense and useless in every other. Node freshness — live, stale, offline — is only reported
while there is a link for it to mean anything.

## Choices worth writing down

**Elliptic curve P-256, not RSA.** A Zero W completes the handshake in a fraction of the
time, every client is one we issue ourselves, and nothing in this system has to
interoperate with anything that cannot do EC.

**The node registry is a JSON file, not a table.** It is administrative: small, edited by a
person a few times a year, and worth reading by eye. It is written atomically at mode 600.
The event journal is a different problem with different volume, and gets the SQLite store
in the next increment.

**The administrative tool is a shell command.** It writes into `/etc` and signs
certificates. The unprivileged web service does not do those things, and an event arriving
over MQTT must never be able to cause them.

**Nothing overwrites without saying so.** `init-ca`, `hub-cert` and the rest refuse to
replace a file that exists; `--force` replaces it and keeps a dated copy. The broker
configuration is an example to copy, not something the tool writes over the top of a broker
that may be serving something else entirely.

## What this increment does not do yet

An event that passes every check here is counted and handed on. Validating the envelope
against the schema, journalling it, deduplicating it and deciding whether it is fresh
enough to act on is the next increment. Until then the hub is deliberately a good listener
that does nothing with what it hears.

The expiry of a certificate is enforced where the handshake happens — at the broker, and
at the TLS layer on both sides. The registry pins identity, not validity dates.

`/api/config` now shows the paths of the hub's certificate files, as it already shows model
and capture paths. Paths, never contents: nothing in the agent or the hub prints key
material, and there is a test that says so.

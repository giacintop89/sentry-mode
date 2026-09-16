# Satellites

A satellite is a second, smaller computer with sensors on it, reporting to the node that
holds the rules. It is not a second Sentry: it reads, it says what it read, and it does
what a signed command allows. Every decision about the house stays on the hub.

This page is how to set one up. What the agent does once it is running is in
[what runs on the board](adr/satellite-agent.md); why the names look the way they do is in
[naming a source](adr/satellite-identity.md).

## Nothing happens until you switch it on

```yaml
satellites:
  enabled: true
  mqtt:
    host: 192.168.11.10      # the address the satellites use, not a loopback address
    port: 8883
```

With `enabled: false` — the default — the hub holds no broker client, opens no file and
starts no thread for any of this, and needs none of the optional dependencies. Install
them with the `satellites` extra when you are ready:

    pip install -e '.[satellites]'

## The authority, once

Every certificate here is issued by an authority you create and keep. Nothing is trusted
because a public CA signed it, and no node is trusted because its certificate is valid:
being valid is what gets it to the door.

    python scripts/satellite_admin.py init-ca
    python scripts/satellite_admin.py hub-cert --address 192.168.11.10

The address is the one the satellites actually connect to, and an IP address is fine — the
certificate is issued with it as a subject alternative name. There is no option anywhere in
this system for skipping verification; if the address changes, reissue the certificate.

The CA key stays on the hub. Only `ca.crt` is ever copied to a satellite.

## A node, one at a time

Make the key on the node when you can, so the private key never travels:

    # on the satellite
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
        -keyout node.key -out node.csr -subj "/CN=zero-entrance"

    # on the hub
    python scripts/satellite_admin.py sign --node zero-entrance --csr node.csr
    python scripts/satellite_admin.py register --node zero-entrance \
        --certificate /etc/sentry-mode/satellites/zero-entrance.crt --zone entrance
    python scripts/satellite_admin.py approve --node zero-entrance

Registering and approving are separate on purpose. A registered node is known; an approved
node is listened to, and approval pins the record to that exact certificate. Another
certificate from the same authority, for the same name, is still refused.

The name in the certificate is the node's identity. `sign` refuses to put a different one
in, and the broker is configured to use it as the username, so the topic a node writes to,
the name it authenticated as, and the name inside its messages all have to agree.

## The broker

Copy `deploy/satellites/mosquitto.conf.example` to `/etc/mosquitto/conf.d/`, and generate
the access list rather than writing it:

    python scripts/satellite_admin.py acl --out /etc/mosquitto/sentry-acl
    systemctl reload mosquitto

A node writes only under its own name and reads only its own commands. There is no topic a
satellite can publish to that another satellite reads.

Do not replace a broker configuration that is already there; the file above adds a listener
of its own. `deploy/satellites/firewall-policy.md` describes what the satellites should be
able to reach, which is one port on one machine and nothing else.

## Taking a node away

    python scripts/satellite_admin.py revoke --node zero-entrance --reason "sold"
    python scripts/satellite_admin.py acl --out /etc/mosquitto/sentry-acl
    systemctl reload mosquitto

The hub stops believing the node the moment it is revoked: the session ends, the grants
end, its retained snapshot is erased and its sensors are marked unavailable. Do the broker
half as well, and check the connection is really gone — **reloading a broker does not close
connections that are already open.**

## What you will see

The hub reports the link and the nodes separately, because they fail separately. A broker
that is down is one fault, reported as `transport_unavailable`, not fifteen nodes that have
all mysteriously gone offline at the same instant. A node that is connected but quiet is
`stale` before it is `offline`.

Nothing a node reports is acted on before the hub has given it a grant, and a grant belongs
to one connection: after a reconnection the old one is worthless, which is what stops a
node that has been off the air from flooding the hub with news from an hour ago.

The subsystem is built when the dashboard starts, and only when `enabled` is true: a hub
with satellites off imports none of the code above. If the link cannot be started — no
certificate yet, the `satellites` extra not installed, a broker that has never been set
up — the dashboard still comes up with its own cameras and microphones working, and the
reason is reported with the rest of the satellite status. The subsystem is stopped with
the dashboard, which tells the nodes it is going before the socket closes.

A node's own devices are in the same inventory as everything a satellite reports:
`legacy-primary`, `legacy-microphone` and `legacy-speaker` are this board's camera,
microphone and speaker, named without a node prefix because they are not somewhere else.

## The journal

Every event a satellite sends is written to `satellites.store_path`, a SQLite file, and
only then acknowledged. If the hub dies before the write, the broker still holds the
message and delivers it again; if it dies after, the copy that arrives next is recognised
as a duplicate. A message the hub refuses on purpose is acknowledged too, so a malformed
event is not sent for ever.

Being in the journal does not mean being acted on. Only an event that is happening now
reaches a rule. Everything else is kept with its reason:

| Reason | What it means |
|---|---|
| `duplicate` | The same event again, usually a retry. |
| `id_reused`, `sequence_reused` | A different event wearing an identifier already used. A fault on the node. |
| `replayed` | Sent from the node's spool after a disconnection. History, not news. |
| `retained` | A copy the broker kept, delivered to a fresh subscriber. |
| `initial_state` | A sensor saying what it already was when it started. A baseline, not a change. |
| `out_of_order` | Older than something already seen from that source since it booted. |
| `expired` | Older than `limits.accept_within_seconds`. |
| `time_uncertain` | The board's clock is not synchronised, or is ahead of the hub's. The source stays in this state until it sends a new baseline. |
| `rate_limited` | Over the node's budget, or over everyone's. |
| `store_unavailable` | The journal could not be written. The event is **not** acknowledged. |
| `queue_full` | Written down, but the rules were too far behind to take it. Marked `dropped`. |

Heartbeats are not written down. The latest one from each node replaces the one before
and is shown with the node's status.

If the disk is full or the file is damaged, the hub keeps running its own cameras and
rules, reports the journal as unavailable, and stops admitting satellite events it
cannot keep. Nothing is acknowledged that was not written.

To look at the journal, copy it, or trim it:

    python scripts/satellite_admin.py journal
    python scripts/satellite_admin.py journal --snapshot /var/backups/satellites.sqlite3
    python scripts/satellite_admin.py journal --forget-events-older-than 30
    python scripts/satellite_admin.py journal --forget-receipts-older-than 30

Use `--snapshot` rather than copying the file: the journal runs in write-ahead mode, and
a plain copy can be missing the newest events. Forgetting events leaves the receipts in
place, so an old event still cannot be admitted twice; receipts younger than a day are
never forgotten. The hub also trims both once an hour by `journal_days` and `dedup_days`.

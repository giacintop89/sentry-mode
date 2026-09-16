# The satellite journal

Status: accepted, PR-04.

## Decision

Satellite events go through one entry point, `satellites/ingress.py`, and are written to
one SQLite file, `satellites.store_path`, before anything else happens to them.

- **Acknowledged after commit.** The hub turns off automatic MQTT acknowledgement and
  acknowledges each QoS 1 message itself, once the receipt and the journal row are
  committed, or once it has deliberately refused the message. If the write fails,
  nothing is acknowledged and the broker delivers the message again.
- **The database does the deduplicating.** An event identifier is unique per node, and
  a sequence number is unique per source within one `boot_id`. A retry collides and is
  a `duplicate`. The same identifier with different content is `id_reused`, which is
  reported as a fault and is not treated as a retry. The comparison uses a fingerprint
  of what the event says, not of how it travelled.
- **One writer.** One connection behind one lock. WAL mode with `synchronous=FULL`,
  because losing a write we have already acknowledged is the one loss the design
  promises not to have.
- **Only `live` is eligible.** Replayed, retained, opening-snapshot, out-of-order,
  expired and clock-uncertain events are all written down, and none of them reaches a
  rule. The high-water mark is per source and per boot, and an older event never moves
  it backwards.
- **A clock fault lasts until a new baseline.** A reading from a clock that is not
  synchronised, or is ahead of the hub by more than the tolerance, puts the source in
  `time_uncertain`. The source stays there, even if later timestamps look fine, until it
  sends an `initial_state` event. Before that the hub cannot show the source's readings
  are fresh.
- **A bounded queue to the rules.** Eligible events are handed to a queue and delivered
  on a separate thread, so a slow rule never blocks the MQTT client or the writer. A
  full queue is recorded as `queue_full`, and the journal row is marked `dropped`. It is
  never counted as acted on.
- **Two budgets.** A token bucket per node and one shared bucket. A node that floods runs
  out of its own budget first, so the other nodes keep being heard.
- **Heartbeats stay in memory.** Only the latest one per node is kept.
- **Housekeeping.** A background thread renews grants before they lapse. Without it, a
  node's permission to report runs out a few minutes after it connects. The same thread
  trims history and receipts once an hour, each against its own retention.

## Differences from the plan

The plan's minimal schema (§5.3) also lists `nodes`, `sources`, `zones`,
`credential_revocations`, `trigger_runs` and `action_runs`. The first four already live in
the node registry file (see [what it takes to be believed](satellite-trust.md)), and
keeping a second copy here would give two answers to "is this node approved". The two
`*_runs` tables record what a rule did, so they belong to the increment that introduces
the rules (PR-05). Migrations are numbered `.sql` files with a checksum, and a changed
migration makes the journal refuse to open.

Receipts are always kept longer than the replay window. The longest window is one day and
the shortest receipt retention is one day, so no combination of settings breaks this, and
a test checks the two bounds against each other.

## Consequences

- A hub with a full or damaged disk keeps its own cameras and rules. It reports the
  journal as unavailable and stops admitting satellite events. The nodes keep them in
  their spools.
- `scripts/satellite_admin.py journal` copies the file through SQLite's backup API,
  because copying the file under a live WAL can miss the newest transactions.
- Payloads are never logged when validation fails, because they are unvalidated input
  and may quote a person.

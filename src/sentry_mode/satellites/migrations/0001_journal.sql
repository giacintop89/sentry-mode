-- The journal a satellite's events are written to, and the receipts that stop them
-- being written twice. Nothing here belongs to a rule: what an event meant, and what was
-- done about it, is recorded by whatever acts on it.

CREATE TABLE event_receipts (
    node_id     TEXT    NOT NULL,
    event_id    TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL,
    source_id   TEXT    NOT NULL,
    sequence    INTEGER NOT NULL,
    fingerprint TEXT    NOT NULL,
    received_at REAL    NOT NULL,
    PRIMARY KEY (node_id, event_id)
);

-- One identifier per event, and one sequence number per source within a boot. A node that
-- reuses either is not retrying: it is saying two different things with the same name.
CREATE UNIQUE INDEX event_receipts_stream
    ON event_receipts (node_id, boot_id, source_id, sequence);
CREATE INDEX event_receipts_age ON event_receipts (received_at);

CREATE TABLE event_journal (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id            TEXT    NOT NULL,
    source_id          TEXT    NOT NULL,
    event_id           TEXT    NOT NULL,
    boot_id            TEXT    NOT NULL,
    sequence           INTEGER NOT NULL,
    kind               TEXT    NOT NULL,
    value              TEXT,
    unit               TEXT,
    quality            TEXT    NOT NULL,
    clock_status       TEXT    NOT NULL,
    zone               TEXT,
    occurred_at        TEXT    NOT NULL,
    received_at        REAL    NOT NULL,
    received_monotonic REAL    NOT NULL,
    queued_ms          INTEGER NOT NULL DEFAULT 0,
    hub_epoch          INTEGER NOT NULL DEFAULT 0,
    connection_id      TEXT,
    classification     TEXT    NOT NULL,
    eligible           INTEGER NOT NULL DEFAULT 0,
    reason             TEXT,
    outcome            TEXT    NOT NULL DEFAULT 'recorded'
);

CREATE INDEX event_journal_arrival ON event_journal (received_at);
CREATE INDEX event_journal_source ON event_journal (node_id, source_id, received_at);

-- Where a source had got to: the sequence already seen in this boot, whether the opening
-- snapshot has been taken, and whether its clock is currently worth believing.
CREATE TABLE source_marks (
    source_key       TEXT    PRIMARY KEY,
    node_id          TEXT    NOT NULL,
    source_id        TEXT    NOT NULL,
    boot_id          TEXT    NOT NULL,
    high_water       INTEGER NOT NULL DEFAULT -1,
    baseline_taken   INTEGER NOT NULL DEFAULT 0,
    time_state       TEXT    NOT NULL DEFAULT 'ok',
    last_occurred_at TEXT,
    updated_at       REAL    NOT NULL
);

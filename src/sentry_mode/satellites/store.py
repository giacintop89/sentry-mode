"""The journal: one SQLite file, one writer, and a receipt before anything is confirmed.

The rule this module exists to keep is that nothing is acknowledged to a satellite until it
is committed here. A message the hub has not written is a message the node should send
again, so an acknowledgement is the last thing that happens, never the first.

Two constraints do the deduplicating, and they are in the database rather than in Python:
an event identifier is unique per node, and a sequence number is unique per source within
one boot. A retry therefore collides and is recognised; two different events wearing the
same identifier collide as well, which is a fault worth reporting rather than a retry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS = "migrations"
"""The directory of `.sql` files shipped beside this module, applied in order once each."""

WAIT_BANDS = (10, 50, 100, 250, 500, 1000, 2000, 5000, 10_000, 30_000, 60_000)
"""Where one band of waiting ends and the next begins, in milliseconds.

Wider as they go up, because what is being asked is which heap a reading belongs to and not
what its exact millisecond was: a node that is working answers in tens of milliseconds, and
a node that could not send answers in seconds or minutes."""

STORED = "stored"
DUPLICATE = "duplicate"
ID_REUSED = "id_reused"
SEQUENCE_REUSED = "sequence_reused"


class StoreUnavailable(RuntimeError):
    """The journal cannot be read or written: no disk, no permission, or a damaged file."""


@dataclass(frozen=True)
class Mark:
    """How far a source had got, as the journal remembers it."""

    node_id: str
    source_id: str
    boot_id: str
    high_water: int = -1
    baseline_taken: bool = False
    time_state: str = "ok"
    last_occurred_at: str | None = None

    @property
    def key(self) -> str:
        return f"{self.node_id}.{self.source_id}"


@dataclass(frozen=True)
class Entry:
    """One event as the hub decided to record it, whether or not it may act on it."""

    node_id: str
    source_id: str
    event_id: str
    boot_id: str
    sequence: int
    kind: str
    value: object
    unit: str | None
    quality: str
    clock_status: str
    zone: str | None
    occurred_at: str
    received_at: float
    received_monotonic: float
    queued_ms: int
    hub_epoch: int
    connection_id: str | None
    classification: str
    eligible: bool
    reason: str | None

    def fingerprint(self) -> str:
        """What the event says, apart from how it travelled.

        Two copies of one event have the same fingerprint however they arrived; the same
        identifier over different contents does not, which is what tells a retry from a
        node that is reusing identifiers.
        """
        body = json.dumps(
            [
                self.source_id,
                self.boot_id,
                self.sequence,
                self.kind,
                self.value,
                self.unit,
                self.quality,
                self.occurred_at,
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Store:
    """The journal file. One connection, one lock: there is exactly one writer."""

    def __init__(self, path: Path | str, *, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._error: str | None = None

    # -- lifecycle ---------------------------------------------------------------

    def open(self) -> None:
        """Open the file and bring it up to date, or say clearly that it cannot be used."""
        with self._lock:
            if self._connection is not None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                # An event is acknowledged once it is committed, so the commit has to be
                # worth the acknowledgement: a lost write here would be a silent loss.
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                self._connection = connection
                self._migrate(connection)
            except (sqlite3.Error, OSError) as error:
                self._connection = None
                self._error = str(error)
                message = f"the journal at {self.path} is unusable: {error}"
                raise StoreUnavailable(message) from error
            self._error = None

    def close(self) -> None:
        with self._lock:
            connection, self._connection = self._connection, None
            if connection is not None:
                connection.close()

    @property
    def available(self) -> bool:
        return self._connection is not None

    @property
    def error(self) -> str | None:
        return self._error

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreUnavailable(self._error or "the journal is not open")
        return self._connection

    # -- migrations --------------------------------------------------------------

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
            """
        )
        applied = {
            row["version"]: row for row in connection.execute("SELECT * FROM schema_migrations")
        }
        for version, name, sql in migrations():
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            known = applied.get(version)
            if known is not None:
                if known["checksum"] != checksum:
                    raise StoreUnavailable(
                        f"migration {name} has changed since it was applied to {self.path}"
                    )
                continue
            with connection:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at)"
                    " VALUES (?, ?, ?, ?)",
                    (version, name, checksum, self._clock()),
                )
            log.info("applied satellite journal migration %s", name)

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = (
                self._require()
                .execute("SELECT MAX(version) AS version FROM schema_migrations")
                .fetchone()
            )
            return int(row["version"] or 0)

    # -- writing -----------------------------------------------------------------

    def admit(self, entry: Entry, *, mark: Mark | None = None) -> str:
        """Write a receipt, the journal row and the source's new position, or nothing.

        The three belong together: a journal entry without its receipt could be written
        twice, and a receipt without its position would let the next event look older than
        it is. They go in one transaction so that a crash leaves none of them.
        """
        fingerprint = entry.fingerprint()
        with self._lock:
            connection = self._require()
            try:
                with connection:
                    existing = connection.execute(
                        "SELECT fingerprint FROM event_receipts WHERE node_id = ? AND event_id = ?",
                        (entry.node_id, entry.event_id),
                    ).fetchone()
                    if existing is not None:
                        return DUPLICATE if existing["fingerprint"] == fingerprint else ID_REUSED
                    clash = connection.execute(
                        "SELECT event_id FROM event_receipts"
                        " WHERE node_id = ? AND boot_id = ? AND source_id = ? AND sequence = ?",
                        (entry.node_id, entry.boot_id, entry.source_id, entry.sequence),
                    ).fetchone()
                    if clash is not None:
                        return SEQUENCE_REUSED
                    connection.execute(
                        "INSERT INTO event_receipts"
                        " (node_id, event_id, boot_id, source_id, sequence, fingerprint,"
                        "  received_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            entry.node_id,
                            entry.event_id,
                            entry.boot_id,
                            entry.source_id,
                            entry.sequence,
                            fingerprint,
                            entry.received_at,
                        ),
                    )
                    self._write_journal(connection, entry)
                    if mark is not None:
                        self._write_mark(connection, mark)
            except sqlite3.IntegrityError:
                # Two copies raced each other into the same transaction boundary; the
                # constraint did its job and the second copy is a duplicate, not a failure.
                return DUPLICATE
            except sqlite3.Error as error:
                self._error = str(error)
                raise StoreUnavailable(f"the journal could not be written: {error}") from error
        return STORED

    def record_refusal(self, entry: Entry) -> None:
        """Journal something that was refused before it ever earned a receipt.

        A message that is thrown away without a trace is a message nobody can explain
        afterwards, so the reason is written down even though the event is not admitted.
        """
        with self._lock:
            connection = self._require()
            try:
                with connection:
                    self._write_journal(connection, replace(entry, eligible=False))
            except sqlite3.Error as error:
                self._error = str(error)
                raise StoreUnavailable(f"the journal could not be written: {error}") from error

    def _write_journal(self, connection: sqlite3.Connection, entry: Entry) -> None:
        connection.execute(
            "INSERT INTO event_journal"
            " (node_id, source_id, event_id, boot_id, sequence, kind, value, unit, quality,"
            "  clock_status, zone, occurred_at, received_at, received_monotonic, queued_ms,"
            "  hub_epoch, connection_id, classification, eligible, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.node_id,
                entry.source_id,
                entry.event_id,
                entry.boot_id,
                entry.sequence,
                entry.kind,
                json.dumps(entry.value, separators=(",", ":")),
                entry.unit,
                entry.quality,
                entry.clock_status,
                entry.zone,
                entry.occurred_at,
                entry.received_at,
                entry.received_monotonic,
                entry.queued_ms,
                entry.hub_epoch,
                entry.connection_id,
                entry.classification,
                1 if entry.eligible else 0,
                entry.reason,
            ),
        )

    def _write_mark(self, connection: sqlite3.Connection, mark: Mark) -> None:
        connection.execute(
            "INSERT INTO source_marks"
            " (source_key, node_id, source_id, boot_id, high_water, baseline_taken, time_state,"
            "  last_occurred_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(source_key) DO UPDATE SET"
            "  boot_id = excluded.boot_id, high_water = excluded.high_water,"
            "  baseline_taken = excluded.baseline_taken, time_state = excluded.time_state,"
            "  last_occurred_at = excluded.last_occurred_at, updated_at = excluded.updated_at",
            (
                mark.key,
                mark.node_id,
                mark.source_id,
                mark.boot_id,
                mark.high_water,
                1 if mark.baseline_taken else 0,
                mark.time_state,
                mark.last_occurred_at,
                self._clock(),
            ),
        )

    def remember(self, mark: Mark) -> None:
        """Move a source's position on its own, for events that are recorded but not admitted."""
        with self._lock:
            connection = self._require()
            try:
                with connection:
                    self._write_mark(connection, mark)
            except sqlite3.Error as error:
                self._error = str(error)
                raise StoreUnavailable(f"the journal could not be written: {error}") from error

    def outcome(self, node_id: str, event_id: str, outcome: str) -> None:
        """Say what became of an admitted event. A full queue is a fact, not an acceptance."""
        with self._lock:
            connection = self._require()
            with connection:
                connection.execute(
                    "UPDATE event_journal SET outcome = ? WHERE node_id = ? AND event_id = ?",
                    (outcome, node_id, event_id),
                )

    # -- reading -----------------------------------------------------------------

    def mark(self, node_id: str, source_id: str) -> Mark | None:
        with self._lock:
            row = (
                self._require()
                .execute(
                    "SELECT * FROM source_marks WHERE source_key = ?", (f"{node_id}.{source_id}",)
                )
                .fetchone()
            )
        if row is None:
            return None
        return Mark(
            node_id=row["node_id"],
            source_id=row["source_id"],
            boot_id=row["boot_id"],
            high_water=row["high_water"],
            baseline_taken=bool(row["baseline_taken"]),
            time_state=row["time_state"],
            last_occurred_at=row["last_occurred_at"],
        )

    def recent(self, *, limit: int = 50, node_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM event_journal"
        parameters: tuple = ()
        if node_id is not None:
            query += " WHERE node_id = ?"
            parameters = (node_id,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._require().execute(query, (*parameters, int(limit))).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict:
        with self._lock:
            connection = self._require()
            journal = connection.execute("SELECT COUNT(*) AS n FROM event_journal").fetchone()["n"]
            receipts = connection.execute("SELECT COUNT(*) AS n FROM event_receipts").fetchone()[
                "n"
            ]
            reasons = {
                row["reason"]: row["n"]
                for row in connection.execute(
                    "SELECT reason, COUNT(*) AS n FROM event_journal"
                    " WHERE reason IS NOT NULL GROUP BY reason"
                )
            }
        return {"journal": journal, "receipts": receipts, "reasons": reasons}

    def status(self) -> dict:
        """What the journal is doing, including the reason it is doing nothing."""
        if not self.available:
            return {"available": False, "error": self._error, "path": str(self.path)}
        try:
            counts = self.counts()
        except sqlite3.Error as error:  # pragma: no cover - only a damaged file gets here
            return {"available": False, "error": str(error), "path": str(self.path)}
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "available": True,
            "error": None,
            "path": str(self.path),
            "schema_version": self.schema_version,
            "size_bytes": size,
            **counts,
        }

    def waits(self, *, node_id: str | None = None) -> dict:
        """How long the journalled readings waited, as a distribution rather than a number.

        The hub calls a node behind when one reading waited longer than a threshold, and a
        threshold is a number somebody chose. Whether it is in a sensible place is a
        question about where the readings actually are, and this is how to ask it: a
        working node and a node that could not send are two different heaps, and what
        matters is that the line falls between them rather than inside either.
        """
        where = " WHERE queued_ms IS NOT NULL"
        parameters: tuple = ()
        if node_id is not None:
            where += " AND node_id = ?"
            parameters = (node_id,)
        with self._lock:
            connection = self._require()
            total = connection.execute(
                f"SELECT COUNT(*) AS n FROM event_journal{where}", parameters
            ).fetchone()["n"]
            bands = []
            for low, high in zip((0, *WAIT_BANDS), (*WAIT_BANDS, None), strict=True):
                if high is None:
                    query = f"SELECT COUNT(*) AS n FROM event_journal{where} AND queued_ms >= ?"
                    counted = (*parameters, low)
                else:
                    query = (
                        f"SELECT COUNT(*) AS n FROM event_journal{where}"
                        " AND queued_ms >= ? AND queued_ms < ?"
                    )
                    counted = (*parameters, low, high)
                bands.append(
                    {
                        "from_ms": low,
                        "to_ms": high,
                        "count": connection.execute(query, counted).fetchone()["n"],
                    }
                )

            def at(fraction: float) -> int | None:
                # Read out of the sorted column rather than into memory: a journal is
                # allowed to be larger than the machine looking at it.
                if total == 0:
                    return None
                offset = min(total - 1, int(total * fraction))
                row = connection.execute(
                    f"SELECT queued_ms FROM event_journal{where}"
                    " ORDER BY queued_ms LIMIT 1 OFFSET ?",
                    (*parameters, offset),
                ).fetchone()
                return None if row is None else row["queued_ms"]

            spread = {
                name: at(fraction)
                for name, fraction in (("p50", 0.5), ("p90", 0.9), ("p99", 0.99), ("p999", 0.999))
            }
            # The worst is asked for rather than counted to: readings keep arriving while
            # this runs, and a row counted by position is not the last row a moment later.
            spread["max"] = connection.execute(
                f"SELECT MAX(queued_ms) AS worst FROM event_journal{where}", parameters
            ).fetchone()["worst"]
            nodes = {}
            if node_id is None:
                for row in connection.execute(
                    "SELECT node_id, COUNT(*) AS n, MAX(queued_ms) AS worst FROM event_journal"
                    " WHERE queued_ms IS NOT NULL GROUP BY node_id"
                ):
                    nodes[row["node_id"]] = {"count": row["n"], "worst_ms": row["worst"]}
        return {"count": total, "bands": bands, "spread": spread, "nodes": nodes}

    # -- keeping it small --------------------------------------------------------

    def forget_events(self, *, older_than_seconds: float) -> int:
        """Delete visible history. The receipts stay: they are what stops a replay."""
        cutoff = self._clock() - older_than_seconds
        with self._lock:
            connection = self._require()
            with connection:
                cursor = connection.execute(
                    "DELETE FROM event_journal WHERE received_at < ?", (cutoff,)
                )
        return cursor.rowcount

    def forget_receipts(self, *, older_than_seconds: float) -> int:
        """Delete deduplication history, which is only safe outside the replay window."""
        cutoff = self._clock() - older_than_seconds
        with self._lock:
            connection = self._require()
            with connection:
                cursor = connection.execute(
                    "DELETE FROM event_receipts WHERE received_at < ?", (cutoff,)
                )
        return cursor.rowcount

    def snapshot(self, destination: Path | str) -> Path:
        """A copy that is a database, not a file copied from under a live writer.

        Copying the file alone while the write-ahead log holds recent transactions produces
        something that looks like a backup and is missing the newest events, so this goes
        through SQLite's own backup, which takes the log with it.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            connection = self._require()
            try:
                copy = sqlite3.connect(target)
                try:
                    connection.backup(copy)
                finally:
                    copy.close()
            except (sqlite3.Error, OSError) as error:
                raise StoreUnavailable(f"the journal could not be copied: {error}") from error
        return target


def migrations() -> list[tuple[int, str, str]]:
    """Every migration in order, read from the files shipped beside this module."""
    found = []
    directory = files("sentry_mode.satellites") / MIGRATIONS
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        name = path.name
        if not name.endswith(".sql"):
            continue
        found.append((int(name.split("_", 1)[0]), name, path.read_text(encoding="utf-8")))
    return found


__all__ = [
    "DUPLICATE",
    "ID_REUSED",
    "SEQUENCE_REUSED",
    "STORED",
    "Entry",
    "Mark",
    "Store",
    "StoreUnavailable",
    "migrations",
]

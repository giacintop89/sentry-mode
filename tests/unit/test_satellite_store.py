"""The journal: what it keeps, what it refuses to keep twice, and what it says when it cannot."""

import sqlite3

import pytest

from sentry_mode.satellites.store import (
    DUPLICATE,
    ID_REUSED,
    SEQUENCE_REUSED,
    STORED,
    Entry,
    Mark,
    Store,
    StoreUnavailable,
    migrations,
)

BOOT = "6f1c2d3e-4a5b-4c7d-8e9f-0a1b2c3d4e5f"


def entry(**changes) -> Entry:
    fields = {
        "node_id": "zero-entrance",
        "source_id": "pir-1",
        "event_id": "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23",
        "boot_id": BOOT,
        "sequence": 1,
        "kind": "sensor.motion",
        "value": True,
        "unit": None,
        "quality": "valid",
        "clock_status": "synced",
        "zone": "entrance",
        "occurred_at": "2026-09-17T09:00:00+00:00",
        "received_at": 1_000.0,
        "received_monotonic": 10.0,
        "queued_ms": 0,
        "hub_epoch": 1,
        "connection_id": "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21",
        "classification": "live",
        "eligible": True,
        "reason": None,
    }
    fields.update(changes)
    return Entry(**fields)


@pytest.fixture
def store(tmp_path):
    opened = Store(tmp_path / "journal.sqlite3")
    opened.open()
    yield opened
    opened.close()


def test_a_new_file_gets_the_schema_and_says_which_one(store):
    assert store.schema_version == migrations()[-1][0]
    assert store.status()["available"] is True
    assert store.status()["journal"] == 0


def test_opening_twice_does_not_apply_the_schema_twice(tmp_path):
    first = Store(tmp_path / "journal.sqlite3")
    first.open()
    first.admit(entry())
    first.close()
    again = Store(tmp_path / "journal.sqlite3")
    again.open()
    assert again.counts()["journal"] == 1
    again.close()


def test_a_migration_that_changed_after_it_was_applied_is_refused(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    store.open()
    store.close()
    with sqlite3.connect(tmp_path / "journal.sqlite3") as connection:
        connection.execute("UPDATE schema_migrations SET checksum = 'different'")
    with pytest.raises(StoreUnavailable, match="has changed"):
        Store(tmp_path / "journal.sqlite3").open()


def test_the_same_event_twice_is_stored_once(store):
    assert store.admit(entry()) == STORED
    assert store.admit(entry()) == DUPLICATE
    assert store.counts()["journal"] == 1


def test_one_identifier_over_two_different_events_is_a_fault_not_a_retry(store):
    store.admit(entry())
    assert store.admit(entry(value=False, sequence=2)) == ID_REUSED
    assert store.counts()["journal"] == 1


def test_one_sequence_number_used_twice_in_a_boot_is_refused(store):
    store.admit(entry())
    clash = entry(event_id="11111111-2222-4333-8444-555555555555")
    assert store.admit(clash) == SEQUENCE_REUSED


def test_the_same_sequence_in_a_new_boot_is_a_different_event(store):
    store.admit(entry())
    restarted = entry(
        event_id="11111111-2222-4333-8444-555555555555",
        boot_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    )
    assert store.admit(restarted) == STORED


def test_a_refusal_is_written_down_without_earning_a_receipt(store):
    store.record_refusal(entry(classification="historic", eligible=False, reason="expired"))
    assert store.counts() == {"journal": 1, "receipts": 0, "reasons": {"expired": 1}}


def test_a_position_is_only_remembered_when_the_event_was(store):
    mark = Mark(node_id="zero-entrance", source_id="pir-1", boot_id=BOOT, high_water=1)
    store.admit(entry(), mark=mark)
    assert store.mark("zero-entrance", "pir-1").high_water == 1
    store.admit(entry(), mark=Mark(**{**mark.__dict__, "high_water": 9}))
    assert store.mark("zero-entrance", "pir-1").high_water == 1


def test_what_happened_to_an_event_can_be_corrected_afterwards(store):
    store.admit(entry())
    store.outcome("zero-entrance", entry().event_id, "dropped")
    assert store.recent()[0]["outcome"] == "dropped"


def test_history_and_deduplication_are_forgotten_separately(store):
    store.admit(entry())
    assert store.forget_events(older_than_seconds=-1_000_000) == 1
    assert store.counts() == {"journal": 0, "receipts": 1, "reasons": {}}
    assert store.admit(entry()) == DUPLICATE
    assert store.forget_receipts(older_than_seconds=-1_000_000) == 1
    assert store.admit(entry()) == STORED


def test_a_snapshot_is_a_database_and_not_a_file_copy(store, tmp_path):
    store.admit(entry())
    copy = store.snapshot(tmp_path / "backup" / "journal.sqlite3")
    with sqlite3.connect(copy) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM event_journal").fetchone()[0]
    assert rows == 1


def test_a_journal_that_cannot_be_opened_says_so_instead_of_pretending(tmp_path):
    path = tmp_path / "journal.sqlite3"
    path.write_text("this is not a database")
    store = Store(path)
    with pytest.raises(StoreUnavailable):
        store.open()
    assert store.available is False
    assert store.status() == {"available": False, "error": store.error, "path": str(path)}


def test_writing_to_a_closed_journal_is_refused_not_ignored(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    with pytest.raises(StoreUnavailable):
        store.admit(entry())


def test_the_value_survives_the_round_trip_with_its_type(store):
    store.admit(entry(value=21.5, unit="C", event_id="11111111-2222-4333-8444-555555555555"))
    assert store.recent()[0]["value"] == "21.5"
    assert store.recent(node_id="somebody-else") == []

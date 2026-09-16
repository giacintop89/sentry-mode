"""The door: what counts as happening now, and everything that is recorded but not acted on."""

import uuid
from datetime import datetime, timezone

import pytest

from sentry_mode.satellites.config import LimitsConfig
from sentry_mode.satellites.ingress import REASONS, EventIngress, RateLimiter
from sentry_mode.satellites.store import Store
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

BOOT = "6f1c2d3e-4a5b-4c7d-8e9f-0a1b2c3d4e5f"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def at(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(NOW + offset, tz=timezone.utc).isoformat()


@pytest.fixture
def door(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    store.open()
    sources = SourceRegistry()
    sources.register(
        SourceRecord(
            ref=SourceRef(node="zero-entrance", name="pir-1"),
            kind=SourceKind.SENSOR,
            display_name="PIR",
            origin="satellite",
            zone="entrance",
        )
    )
    ingress = EventIngress(
        store=store, sources=sources, limits=LimitsConfig(), clock=Clock(), monotonic=Clock(5.0)
    )
    yield ingress
    store.close()


def envelope(*, sequence=1, event_id=None, boot_id=BOOT, occurred_at=None, **overrides) -> dict:
    event = {
        "event_id": event_id or str(uuid.uuid4()),
        "node_id": overrides.pop("node_id", "zero-entrance"),
        "source_id": overrides.pop("source_id", "pir-1"),
        "boot_id": boot_id,
        "sequence": sequence,
        "kind": "sensor.motion",
        "occurred_at": occurred_at or at(-1),
        "clock_status": overrides.pop("clock_status", "synced"),
        "value": overrides.pop("value", True),
        "quality": "valid",
    }
    delivery = {
        "connection_id": "d3b07384-d9a0-4f1e-9c2b-3e1f7a5c9b21",
        "hub_epoch": 1,
        "grant_id": "0f6c4a1e-9a5b-4c2d-8e11-5b7c9d0a1f23",
        **overrides,
    }
    return {"schema_version": 1, "event": event, "delivery": delivery}


def test_a_fresh_event_is_live_eligible_and_carries_the_hubs_answers(door):
    receipt = door.accept("zero-entrance", envelope())
    assert (receipt.classification, receipt.eligible, receipt.stored) == ("live", True, True)
    assert receipt.event.ref.id == "zero-entrance.pir-1"
    assert receipt.event.zone == "entrance"
    assert (receipt.event.received_at, receipt.event.received_monotonic) == (NOW, 5.0)


def test_a_retry_is_a_duplicate_and_is_not_acted_on_twice(door):
    document = envelope()
    assert door.accept("zero-entrance", document).eligible
    again = door.accept("zero-entrance", document)
    assert (again.eligible, again.reason, again.settle) == (False, "duplicate", True)


def test_a_duplicate_is_still_recognised_after_the_hub_restarts(door, tmp_path):
    document = envelope()
    door.accept("zero-entrance", document)
    door.store.close()
    door.store.open()
    assert door.accept("zero-entrance", document).reason == "duplicate"


def test_the_same_identifier_with_different_contents_is_refused(door):
    first = envelope()
    door.accept("zero-entrance", first)
    second = envelope(event_id=first["event"]["event_id"], sequence=2, value=False)
    assert door.accept("zero-entrance", second).reason == "id_reused"


def test_an_event_older_than_one_already_seen_is_history(door):
    door.accept("zero-entrance", envelope(sequence=5))
    late = door.accept("zero-entrance", envelope(sequence=3))
    assert (late.classification, late.reason, late.stored) == ("historic", "out_of_order", True)
    assert door.store.mark("zero-entrance", "pir-1").high_water == 5


def test_a_restarted_board_starts_its_sequence_again(door):
    door.accept("zero-entrance", envelope(sequence=5))
    rebooted = envelope(sequence=0, boot_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    assert door.accept("zero-entrance", rebooted).eligible


def test_a_replayed_event_is_kept_and_never_acted_on(door):
    receipt = door.accept("zero-entrance", envelope(replayed=True, queued_ms=90_000))
    assert (receipt.reason, receipt.stored, receipt.eligible) == ("replayed", True, False)


def test_a_retained_copy_is_not_news(door):
    assert door.accept("zero-entrance", envelope(), retained=True).reason == "retained"


def test_the_opening_snapshot_is_a_baseline_and_not_a_transition(door):
    receipt = door.accept("zero-entrance", envelope(initial_state=True))
    assert (receipt.classification, receipt.eligible) == ("initial", False)
    assert door.store.mark("zero-entrance", "pir-1").baseline_taken is True


def test_an_event_too_old_to_be_news_has_expired(door):
    stale = envelope(occurred_at=at(-LimitsConfig().accept_within_seconds - 1))
    assert door.accept("zero-entrance", stale).reason == "expired"


@pytest.mark.parametrize(
    "changes",
    [{"clock_status": "unsynced"}, {"clock_status": "unknown"}, {"occurred_at": at(60)}],
    ids=["unsynced", "unknown", "from-the-future"],
)
def test_a_clock_that_cannot_be_believed_makes_the_reading_uncertain(door, changes):
    assert door.accept("zero-entrance", envelope(**changes)).reason == "time_uncertain"


def test_a_clock_fault_lasts_until_the_source_takes_a_new_baseline(door):
    door.accept("zero-entrance", envelope(sequence=1, clock_status="unsynced"))
    assert door.accept("zero-entrance", envelope(sequence=2)).reason == "time_uncertain"
    door.accept("zero-entrance", envelope(sequence=3, initial_state=True))
    assert door.accept("zero-entrance", envelope(sequence=4)).eligible


def test_a_node_cannot_report_for_another_node(door):
    assert door.accept("zero-entrance", envelope(node_id="zero-garden")).reason == (
        "identity_mismatch"
    )


def test_a_sensor_nobody_registered_is_refused_before_the_journal(door):
    receipt = door.accept("zero-entrance", envelope(source_id="pir-9"))
    assert (receipt.reason, receipt.stored) == ("unregistered_source", False)
    assert door.store.counts()["journal"] == 0


def test_a_disabled_sensor_is_not_listened_to(door):
    door.sources.set_state(SourceRef(node="zero-entrance", name="pir-1"), SourceState.DISABLED)
    assert door.accept("zero-entrance", envelope()).reason == "source_disabled"


def test_a_malformed_envelope_is_refused_without_quoting_it(door, caplog):
    document = envelope()
    document["event"]["sequence"] = -1
    document["event"]["value"] = "a secret someone said"
    assert door.accept("zero-entrance", document).reason == "invalid_envelope"
    assert "a secret someone said" not in caplog.text


def test_an_event_the_journal_could_not_keep_is_not_acknowledged(door):
    door.store.close()
    receipt = door.accept("zero-entrance", envelope())
    assert (receipt.reason, receipt.settle, receipt.eligible) == ("store_unavailable", False, False)


def test_every_reason_the_door_gives_is_one_it_declares(door):
    given = {
        door.accept("zero-entrance", envelope(replayed=True)).reason,
        door.accept("zero-entrance", envelope(), retained=True).reason,
        door.accept("zero-entrance", envelope(initial_state=True)).reason,
        door.accept("zero-entrance", envelope(source_id="pir-9")).reason,
    }
    assert given <= set(REASONS)


# -- budgets --------------------------------------------------------------------


def test_one_noisy_node_runs_out_before_it_can_silence_the_others():
    clock = Clock(0.0)
    limiter = RateLimiter(LimitsConfig(events_per_minute=3, total_events_per_minute=5), clock=clock)
    assert [limiter.allow("noisy") for _ in range(4)] == [None, None, None, "node"]
    assert [limiter.allow("quiet") for _ in range(2)] == [None, None]
    assert limiter.allow("third") == "hub"


def test_a_budget_fills_up_again_with_time():
    clock = Clock(0.0)
    limiter = RateLimiter(LimitsConfig(events_per_minute=60), clock=clock)
    for _ in range(60):
        assert limiter.allow("zero-entrance") is None
    assert limiter.allow("zero-entrance") == "node"
    clock.now = 1.0
    assert limiter.allow("zero-entrance") is None


def test_limits_that_contradict_each_other_are_refused():
    with pytest.raises(ValueError, match="one node"):
        LimitsConfig(events_per_minute=100, total_events_per_minute=10)


def test_no_setting_forgets_a_receipt_while_its_event_could_still_arrive():
    fields = LimitsConfig.model_fields
    longest_window = next(
        rule.le for rule in fields["accept_within_seconds"].metadata if hasattr(rule, "le")
    )
    shortest_memory = next(rule.ge for rule in fields["dedup_days"].metadata if hasattr(rule, "ge"))
    assert shortest_memory * 86400 >= longest_window

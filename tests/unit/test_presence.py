"""A known device arriving or leaving: what the hub concludes, and what it refuses to.

Presence reaches the hub as ordinary events from satellites that scan for it, so the
rules here are about combining observers and about never concluding anything from a
scanner that cannot see. The Wi-Fi half is the shape of a router that could answer the
same question; no router is supported, and these tests hold that line too.
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from sentry_mode.presence.wifi import (
    Client,
    ProviderError,
    Snapshot,
    SnapshotProvider,
    WifiPresence,
    normalize,
)
from sentry_mode.satellites.api import DRIVERS
from sentry_mode.satellites.ingress import NormalizedEvent
from sentry_mode.sentry.config import PresenceStateTrigger, RuleV2, TelegramAction
from sentry_mode.sentry.engine import Simulated
from sentry_mode.sentry.resources import ResourcePlanner
from sentry_mode.sentry.triggers import RuleState, presence_fires
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry

sys.path.insert(0, str(Path(__file__).parent))  # the engine fixture lives beside this file

from test_correlation import engine, log, use  # noqa: E402, F401 - a fixture and two helpers

HALL = "zero-hall.phone"
GARAGE = "zero-garage.phone"
PIR = "zero-entrance.pir"
PHONE = "AA:BB:CC:DD:EE:FF"


def heard(source_id, value, *, at=0.0, quality="valid", kind="presence.state"):
    return Simulated(
        ref=SourceRef.parse(source_id),
        kind=kind,
        value=value,
        quality=quality,
        received_monotonic=at,
    )


def arrival(**fields):
    return PresenceStateTrigger(source_id=HALL, **fields)


def presence_rule(trigger=None, **fields):
    return RuleV2(
        id=fields.pop("rule_id", "phone-home"),
        name=fields.pop("name", "Phone home"),
        trigger=trigger or arrival(),
        cooldown_seconds=fields.pop("cooldown_seconds", 0),
        actions=[TelegramAction(text="The phone is home.")],
        **fields,
    )


# -- one device, one or more observers ---------------------------------------------------


def test_a_device_that_turns_up_sets_off_the_rule_that_waits_for_it():
    state = RuleState()
    assert presence_fires(arrival(), state, heard(HALL, "present")) is True
    assert state.phase == "present"


def test_saying_it_again_is_not_another_arrival():
    state, trigger = RuleState(), arrival()
    assert presence_fires(trigger, state, heard(HALL, "present")) is True
    assert presence_fires(trigger, state, heard(HALL, "present", at=30)) is False


def test_leaving_and_coming_back_is_an_arrival_again():
    state, trigger = RuleState(), arrival()
    presence_fires(trigger, state, heard(HALL, "present"))
    assert presence_fires(trigger, state, heard(HALL, "absent", at=200)) is False
    assert presence_fires(trigger, state, heard(HALL, "present", at=400)) is True


def test_one_observer_that_still_sees_it_keeps_the_device_here():
    state, trigger = RuleState(), arrival(state="absent", observers=(GARAGE,))
    presence_fires(trigger, state, heard(HALL, "present"))
    presence_fires(trigger, state, heard(GARAGE, "present"))
    assert presence_fires(trigger, state, heard(HALL, "absent", at=200)) is False
    assert state.phase == "present"
    assert presence_fires(trigger, state, heard(GARAGE, "absent", at=210)) is True
    assert state.phase == "absent"


def test_a_scanner_that_cannot_see_leaves_the_answer_unknown():
    state, trigger = RuleState(), arrival(state="absent", observers=(GARAGE,))
    presence_fires(trigger, state, heard(HALL, "present"))
    presence_fires(trigger, state, heard(GARAGE, "present"))
    presence_fires(trigger, state, heard(HALL, "absent", at=200))
    assert presence_fires(trigger, state, heard(GARAGE, None, at=205, quality="unknown")) is False
    assert state.phase == "unknown"
    assert presence_fires(trigger, state, heard(GARAGE, "absent", at=260)) is True


def test_an_observer_that_has_not_spoken_yet_holds_an_absence_back():
    state = RuleState()
    trigger = arrival(state="absent", observers=(GARAGE,))
    assert presence_fires(trigger, state, heard(HALL, "absent")) is False
    assert state.phase == "unknown"


def test_a_first_arrival_needs_only_the_one_observer_that_sees_it():
    state = RuleState()
    trigger = arrival(observers=(GARAGE,))
    assert presence_fires(trigger, state, heard(GARAGE, "present")) is True


def test_a_source_the_rule_never_named_is_not_an_observer():
    state, trigger = RuleState(), arrival()
    assert presence_fires(trigger, state, heard(GARAGE, "present")) is False
    assert state.seen == {}


def test_a_reading_of_another_kind_is_not_presence():
    state, trigger = RuleState(), arrival()
    assert presence_fires(trigger, state, heard(HALL, "present", kind="motion.pir")) is False


def test_a_word_that_is_neither_present_nor_absent_says_nothing():
    state, trigger = RuleState(), arrival(state="absent")
    assert presence_fires(trigger, state, heard(HALL, "maybe")) is False
    assert state.phase == "unknown"


# -- what the planner lets a rule ask for ------------------------------------------------


def registry(*records):
    made = SourceRegistry()
    for ref, kind in records:
        made.register(
            SourceRecord(
                ref=SourceRef.parse(ref),
                kind=kind,
                display_name=ref,
                zone="hall",
                origin="local" if SourceRef.parse(ref).is_local else "satellite",
                state=SourceState.READY,
            )
        )
    return made


def plan_for(trigger, *records):
    sources = registry(*records or ((HALL, SourceKind.PRESENCE),))
    planner = ResourcePlanner(sources, satellites_enabled=True)
    return planner.plan([presence_rule(trigger)])


def test_a_presence_rule_can_be_armed_and_every_observer_is_watched():
    plan = plan_for(
        arrival(observers=(GARAGE,)),
        (HALL, SourceKind.PRESENCE),
        (GARAGE, SourceKind.PRESENCE),
    )
    assert plan.problems == ()
    assert set(plan.watched) == {HALL, GARAGE}
    assert plan.detector is False and plan.camera is False


def test_this_hub_scans_for_nothing_itself():
    plan = plan_for(
        PresenceStateTrigger(source_id="legacy-presence"),
        ("legacy-presence", SourceKind.PRESENCE),
    )
    assert "watches for no devices; only satellites do" in plan.problems[0].message


def test_a_motion_sensor_is_not_a_device_to_look_for():
    plan = plan_for(PresenceStateTrigger(source_id=PIR), (PIR, SourceKind.SENSOR))
    assert "presence" in plan.problems[0].message


def test_an_observer_that_is_not_a_source_here_is_named():
    plan = plan_for(arrival(observers=(GARAGE,)), (HALL, SourceKind.PRESENCE))
    assert GARAGE in plan.problems[0].message


def test_a_device_is_watched_by_each_of_its_observers_once():
    trigger = arrival(observers=(GARAGE,))
    assert trigger.observers == (GARAGE,)
    with pytest.raises(ValueError, match="named once"):
        arrival(observers=(HALL,))
    with pytest.raises(ValueError, match="at most eight"):
        arrival(observers=tuple(f"zero-{n}.phone" for n in range(8)))


# -- the engine --------------------------------------------------------------------------


def presence_event(source_id, value, *, quality="valid"):
    return NormalizedEvent(
        ref=SourceRef.parse(source_id),
        event_id=str(uuid.uuid4()),
        boot_id=str(uuid.uuid4()),
        sequence=1,
        kind="presence.state",
        value=value,
        unit=None,
        quality=quality,
        clock_status="synced",
        zone="hall",
        occurred_at=datetime.now(timezone.utc),
        received_at=time.time(),
        received_monotonic=time.monotonic(),
        queued_ms=0,
        hub_epoch=1,
        connection_id="c1",
        classification="live",
        eligible=True,
        reason=None,
    )


def watching(engine, *ids):  # noqa: F811
    for one in ids:
        engine.sources.register(
            SourceRecord(
                ref=SourceRef.parse(one),
                kind=SourceKind.PRESENCE,
                display_name=one,
                zone="hall",
                origin="satellite",
                state=SourceState.READY,
            )
        )


def test_the_hub_says_the_phone_is_home(engine):  # noqa: F811
    watching(engine, HALL)
    use(engine, presence_rule(), test_mode=True)
    engine.arm()
    engine.observe_event(presence_event(HALL, "present"))
    assert log(engine, "would_run") == ["Telegram: The phone is home."]


def test_a_scanner_with_nothing_to_say_runs_nothing(engine):  # noqa: F811
    watching(engine, HALL)
    use(engine, presence_rule(), test_mode=True)
    engine.arm()
    engine.observe_event(presence_event(HALL, None, quality="unknown"))
    assert log(engine, "would_run") == []


def test_the_hub_waits_for_both_nodes_before_calling_it_gone(engine):  # noqa: F811
    watching(engine, HALL, GARAGE)
    trigger = arrival(state="absent", observers=(GARAGE,))
    use(engine, presence_rule(trigger, name="Phone gone"), test_mode=True)
    engine.arm()
    engine.observe_event(presence_event(HALL, "present"))
    engine.observe_event(presence_event(GARAGE, "present"))
    engine.observe_event(presence_event(HALL, "absent"))
    assert log(engine, "would_run") == []
    engine.observe_event(presence_event(GARAGE, "absent"))
    assert log(engine, "would_run") == ["Telegram: The phone is home."]


def test_a_node_may_declare_a_bluetooth_scanner():
    assert "ble" in DRIVERS


def test_the_rule_editor_offers_the_trigger_it_can_save():
    page = files("sentry_mode").joinpath("sentry.html").read_text()
    script = files("sentry_mode").joinpath("sentry.js").read_text()
    assert '<option value="presence_state">' in page
    assert '<option value="presence.state">' in page
    assert 'id="rule-observers"' in page
    assert "presence_state" in script


# -- presence read from a router ---------------------------------------------------------


class Router:
    """A provider that says whatever a test wants, including that it cannot answer."""

    name = "router"

    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.looks = 0

    def look(self):
        self.looks += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


def at(now, *clients):
    return Snapshot(observed_at=now, clients=tuple(clients))


def reading(provider, *, now=1000.0, **fields):
    hub = WifiPresence(provider, {HALL: PHONE}, clock=lambda: now, **fields)
    return hub.read()[0]


def test_a_device_on_the_radio_right_now_is_present():
    found = reading(Router(at(1000.0, Client(mac=PHONE, associated=True))))
    assert (found.source_id, found.value, found.quality) == (HALL, "present", "valid")
    assert "associated" in found.reason


def test_a_lease_is_a_promise_about_an_address_not_about_a_device():
    lease = Client(mac=PHONE, associated=False, leased_until=9_000_000.0)
    found = reading(Router(at(1000.0, lease)))
    assert (found.value, found.quality) == ("absent", "valid")
    assert found.reason == "holds a lease but is not associated"


def test_a_device_the_router_never_heard_of_is_away():
    found = reading(Router(at(1000.0)))
    assert found.value == "absent"
    assert "knows no such device" in found.reason


def test_an_answer_that_has_been_sitting_around_is_no_answer():
    found = reading(Router(at(800.0, Client(mac=PHONE, associated=True))))
    assert (found.value, found.quality) == (None, "unavailable")
    assert "200 s ago" in found.reason


def test_a_router_whose_clock_is_ahead_is_not_believed():
    found = reading(Router(at(1200.0, Client(mac=PHONE, associated=True))))
    assert (found.value, found.quality) == (None, "unavailable")
    assert "in the future" in found.reason


def test_a_router_that_cannot_be_asked_tells_the_hub_nothing():
    found = reading(Router(error=ProviderError("timed out")))
    assert (found.value, found.quality) == (None, "unavailable")
    assert found.reason == "router could not be asked: timed out"


def test_the_address_stays_between_the_hub_and_the_router():
    findings = reading(Router(at(1000.0, Client(mac=PHONE, associated=True, hostname="phone"))))
    assert PHONE not in repr(findings)


def test_one_address_has_one_spelling():
    assert normalize("aa-bb-cc-dd-ee-ff") == PHONE
    with pytest.raises(ValueError, match="not a MAC"):
        normalize("the-phone")
    with pytest.raises(ValueError):
        WifiPresence(Router(), {HALL: "nope"})


def test_a_dump_is_read_the_way_it_was_written(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(
        json.dumps(
            {
                "observed_at": 1000.0,
                "clients": [
                    {"mac": "aa:bb:cc:dd:ee:ff", "associated": True, "hostname": "phone"},
                    {"mac": "11:22:33:44:55:66", "lease_expires_at": 2000.0},
                ],
            }
        )
    )
    snapshot = SnapshotProvider(path).look()
    assert snapshot.observed_at == 1000.0
    assert snapshot.find(PHONE) == Client(mac=PHONE, associated=True, hostname="phone")
    assert snapshot.find("11:22:33:44:55:66").associated is False
    assert reading(SnapshotProvider(path)).value == "present"


def test_an_undated_dump_is_as_old_as_its_file(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"clients": [{"mac": PHONE, "associated": True}]}))
    snapshot = SnapshotProvider(path).look()
    assert abs(snapshot.observed_at - time.time()) < 60
    assert reading(SnapshotProvider(path), now=time.time()).value == "present"


def test_a_file_that_is_not_a_client_table_is_refused(tmp_path):
    missing = SnapshotProvider(tmp_path / "gone.json")
    with pytest.raises(ProviderError, match="cannot be read"):
        missing.look()
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(ProviderError, match="not the JSON"):
        SnapshotProvider(broken).look()
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"clients": [{"host": "phone"}]}))
    with pytest.raises(ProviderError, match="without an address"):
        SnapshotProvider(wrong).look()
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"clients": [{"mac": "zz:zz:zz:zz:zz:zz"}]}))
    with pytest.raises(ProviderError, match="not a MAC"):
        SnapshotProvider(odd).look()


def test_every_device_hears_the_same_bad_news():
    hub = WifiPresence(
        Router(error=ProviderError("no route to host")),
        {HALL: PHONE, GARAGE: "11:22:33:44:55:66"},
        clock=lambda: 1000.0,
    )
    assert [one.source_id for one in hub.read()] == [HALL, GARAGE]
    assert {one.value for one in hub.read()} == {None}

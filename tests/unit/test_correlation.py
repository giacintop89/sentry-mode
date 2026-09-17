"""A sensor event confirmed by a camera, and what a failing source does to the rest."""

import threading
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from sentry_mode.config import Settings
from sentry_mode.satellites.ingress import NormalizedEvent
from sentry_mode.sentry.config import (
    TRIGGER_SOURCE,
    PhotoAction,
    RuleV2,
    SensorEventTrigger,
    SentryConfigV2,
    SequenceTrigger,
    TelegramAction,
    TTSAction,
    VisionTrigger,
)
from sentry_mode.sentry.correlation import sequence_event, sequence_sample
from sentry_mode.sentry.engine import Sentry, Simulated
from sentry_mode.sentry.migration import obstacles
from sentry_mode.sentry.resources import DETECTOR, ResourcePlanner, resolved
from sentry_mode.sentry.triggers import RuleState
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.detection import Detection

PIR = "zero-entrance.pir"
CAMERA = "legacy-primary"
PERSON = [Detection("person", 0.9, (0.4, 0.4, 0.6, 0.6))]


def sequence(within=5.0, hits=2, **fields):
    return SequenceTrigger(
        within_seconds=within,
        steps=(
            SensorEventTrigger(source_id=PIR, kind="motion.pir"),
            VisionTrigger(
                source_id=fields.pop("camera", CAMERA),
                object="person",
                consecutive_detections=hits,
                rearm_after_absence_seconds=fields.pop("absence", 10),
            ),
        ),
        **fields,
    )


def confirmed_rule(actions=None, **fields):
    return RuleV2(
        id=fields.pop("rule_id", "entrance-confirmed"),
        name=fields.pop("name", "Entrance confirmed"),
        trigger=fields.pop("trigger", None) or sequence(),
        cooldown_seconds=fields.pop("cooldown_seconds", 0),
        actions=actions
        or [
            PhotoAction(source_id=TRIGGER_SOURCE),
            TelegramAction(text="Movement confirmed by a person at the entrance."),
        ],
        **fields,
    )


def pir(at, value=True, quality="valid", zone="entrance"):
    return Simulated(
        ref=SourceRef.parse(PIR),
        kind="motion.pir",
        value=value,
        quality=quality,
        received_monotonic=at,
        zone=zone,
        event_id=f"pir-{at}",
    )


class Run:
    """One sequence rule, fed by hand on a clock of our own."""

    def __init__(self, trigger=None, cooldown=0.0):
        self.trigger = trigger or sequence()
        self.state = RuleState()
        self.cooldown = cooldown
        self.notes = []

    def motion(self, at, **fields):
        self.notes += sequence_event(self.trigger, self.state, pir(at, **fields), "entrance")

    def seen(self, at, count=1):
        self.notes += sequence_sample(self.trigger, self.state, count, at, self.cooldown)

    def kinds(self):
        return [kind for kind, _ in self.notes]


# -- the sequence itself -----------------------------------------------------------------


def test_a_person_within_the_window_confirms_the_motion():
    run = Run()
    run.motion(0)
    run.seen(1)
    run.seen(1.5)
    assert run.kinds() == ["waiting", "detected", "triggered"]
    assert "1.5 s after zero-entrance.pir" in run.notes[-1][1]
    assert run.state.candidate is None


def test_a_person_after_the_window_confirms_nothing():
    run = Run()
    run.motion(0)
    run.seen(5.5)
    run.seen(6)
    assert "triggered" not in run.kinds()
    assert "expired" in run.kinds()


def test_repeated_motion_does_not_extend_the_window():
    run = Run()
    run.motion(0)
    run.motion(3)
    run.motion(4.9)
    assert run.state.candidate.deadline == 5
    run.seen(5.2)
    run.seen(5.6)
    assert "triggered" not in run.kinds()
    run.motion(6)  # after it ran out, motion opens a new one
    run.seen(6.5)
    run.seen(7)
    assert run.kinds().count("waiting") == 2 and run.kinds()[-1] == "triggered"


def test_only_sightings_after_the_motion_count():
    run = Run()
    run.seen(-1)
    run.seen(-0.5)
    run.motion(0)
    run.seen(0)  # received at the same moment: not after it
    run.seen(0.5)
    assert "triggered" not in run.kinds()
    run.seen(1)
    assert run.kinds()[-1] == "triggered"


def test_a_late_sensor_event_is_not_a_new_motion():
    run = Run()
    run.motion(10)
    run.seen(16)  # expires the window opened at 10
    run.motion(9)  # delivered late: it happened before the window that already ran out
    assert run.state.candidate is None
    assert run.kinds().count("waiting") == 1


def test_the_confirmation_is_used_once_and_rearms_only_after_a_real_absence():
    run = Run()
    run.motion(0)
    run.seen(0.5)
    run.seen(1)
    assert run.kinds().count("triggered") == 1
    run.motion(2)  # the same person is still there
    assert run.kinds()[-1] == "skipped" and run.state.candidate is None
    run.seen(3, count=0)
    run.seen(12.9, count=0)
    assert run.state.latched
    run.seen(13, count=0)
    assert run.kinds()[-1] == "rearmed"
    run.motion(14)
    run.seen(14.5)
    run.seen(15)
    assert run.kinds().count("triggered") == 2


def test_a_doubtful_reading_drops_the_waiting_motion():
    run = Run()
    run.motion(0)
    run.motion(1, value=None, quality="unavailable")
    run.seen(1.5)
    run.seen(2)
    assert "cancelled" in run.kinds() and "triggered" not in run.kinds()


def test_a_confirmation_during_the_cooldown_is_consumed_but_does_nothing():
    run = Run(cooldown=60)
    run.state.last_trigger = 0
    run.motion(1)
    run.seen(1.5)
    run.seen(2)
    assert run.kinds()[-1] == "skipped" and run.state.candidate is None
    assert not run.state.latched


def test_a_sequence_is_a_sensor_then_a_camera():
    with pytest.raises(ValidationError, match="a sensor event, then a camera detection"):
        SequenceTrigger.model_validate(
            {
                "steps": [
                    {"type": "vision", "object": "person"},
                    {"type": "sensor_event", "source_id": PIR, "kind": "motion.pir"},
                ]
            }
        )
    with pytest.raises(ValidationError):
        sequence(within=0.5)
    with pytest.raises(ValidationError):
        sequence(within=120)


def test_trigger_source_is_the_confirming_camera():
    rule = confirmed_rule(trigger=sequence(camera="zero-entrance.camera"))
    photo = resolved(rule)[0]
    assert photo.source_id == "zero-entrance.camera"


def test_the_first_version_cannot_express_a_sequence():
    config = SentryConfigV2(rules=[confirmed_rule()])
    assert any("sequence" in obstacle for obstacle in obstacles(config))


# -- planning ----------------------------------------------------------------------------


def registry_with(zone_sensor="entrance", zone_camera="entrance"):
    registry = SourceRegistry()
    for source_id, kind, zone in (
        (PIR, SourceKind.SENSOR, zone_sensor),
        ("legacy-primary", SourceKind.CAMERA, zone_camera),
    ):
        registry.register(
            SourceRecord(
                ref=SourceRef.parse(source_id),
                kind=kind,
                display_name=source_id,
                zone=zone,
                origin="local" if source_id == CAMERA else "satellite",
                state=SourceState.READY,
            )
        )
    return ResourcePlanner(registry, satellites_enabled=True)


def test_the_planner_watches_both_steps_and_needs_the_detector():
    plan = registry_with().plan([confirmed_rule()])
    assert plan.ok, plan.problems
    assert plan.detector and plan.camera
    assert plan.watched == {PIR}
    assert plan.needs["entrance-confirmed"] == {PIR, CAMERA, DETECTOR}


@pytest.mark.parametrize(
    "zones,problem",
    [
        (
            ("entrance", "garden"),
            "zero-entrance.pir is in entrance and legacy-primary is in garden",
        ),
        (("entrance", None), "legacy-primary has no zone"),
    ],
)
def test_the_same_zone_is_checked_before_arming(zones, problem):
    plan = registry_with(*zones).plan([confirmed_rule(trigger=sequence(same_zone=True))])
    assert [p.message for p in plan.problems] == [f"the same zone is required, but {problem}"]
    assert registry_with(*zones).plan([confirmed_rule()]).ok


def test_capture_time_is_refused_until_a_camera_can_prove_it():
    plan = registry_with().plan([confirmed_rule(trigger=sequence(time_basis="capture"))])
    assert "timing by capture time is not available" in plan.problems[0].message


# -- armed -------------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    video = MagicMock()
    video.status.return_value = {
        "capture_running": True,
        "error": None,
        "detection": {"enabled": True, "error": None},
    }
    video.latest_capture.return_value = None
    settings = Settings(
        sentry_state_file=tmp_path / "sentry.json",
        captures_directory=tmp_path / "captures",
        sounds_directory=tmp_path / "sounds",
    )
    sentry = Sentry(settings, video, threading.Lock())
    sentry.sources.register(
        SourceRecord(
            ref=SourceRef.parse(PIR),
            kind=SourceKind.SENSOR,
            display_name="PIR",
            zone="entrance",
            origin="satellite",
            state=SourceState.READY,
        )
    )
    sentry.planner = ResourcePlanner(sentry.sources, satellites_enabled=True)
    try:
        yield sentry
    finally:
        sentry.disarm()


def event(at=None, value=True, quality="valid"):
    return NormalizedEvent(
        ref=SourceRef.parse(PIR),
        event_id=str(uuid.uuid4()),
        boot_id=str(uuid.uuid4()),
        sequence=1,
        kind="motion.pir",
        value=value,
        unit=None,
        quality=quality,
        clock_status="synced",
        zone="entrance",
        occurred_at=datetime.now(timezone.utc),
        received_at=time.time(),
        received_monotonic=time.monotonic() if at is None else at,
        queued_ms=0,
        hub_epoch=1,
        connection_id="c1",
        classification="live",
        eligible=True,
        reason=None,
    )


def use(engine, *rules, **settings):
    engine.update_v2(SentryConfigV2(rules=list(rules), **settings), engine.revision)


def log(engine, kind):
    return [e["message"] for e in engine.status()["events"] if e["kind"] == kind]


def rule_state(engine, rule_id):
    return next(r for r in engine.status_v2()["rules"] if r["id"] == rule_id)


def test_pir_then_person_then_photo_then_telegram(engine):
    use(engine, confirmed_rule(), test_mode=True)
    engine.arm()
    motion = event()
    engine.observe_event(motion)
    assert rule_state(engine, "entrance-confirmed")["state"] == "waiting"
    assert 0 < rule_state(engine, "entrance-confirmed")["waiting_seconds_left"] <= 5
    assert log(engine, "would_run") == []
    now = time.monotonic()
    engine.observe(PERSON, now)
    engine.observe(PERSON, now + 0.01)
    assert list(reversed(log(engine, "would_run"))) == [
        "Photo from the triggering camera",
        "Telegram: Movement confirmed by a person at the entrance.",
    ]
    assert rule_state(engine, "entrance-confirmed")["state"] == "latched"


def test_the_actions_know_both_the_motion_and_the_sighting(engine):
    use(engine, confirmed_rule(actions=[TTSAction(text="Hello")]))
    ran = []
    engine._step = lambda rule, action, deadline, context=None: ran.append(context)
    engine.arm()
    motion = event()
    engine.observe_event(motion)
    now = time.monotonic()
    engine.observe(PERSON, now)
    engine.observe(PERSON, now + 0.01)
    deadline = time.monotonic() + 3
    while not ran and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ran[0].origin == (motion.event_id, f"vision:{CAMERA}")
    assert ran[0].zone == "entrance"


def test_a_person_seen_before_arming_the_motion_is_not_a_confirmation(engine):
    use(engine, confirmed_rule(), test_mode=True)
    engine.arm()
    now = time.monotonic()
    engine.observe(PERSON, now)
    engine.observe(PERSON, now + 0.01)
    engine.observe_event(event(at=now + 0.02))
    engine.observe(PERSON, now + 0.03)
    assert log(engine, "would_run") == []
    engine.observe(PERSON, now + 0.04)
    assert len(log(engine, "would_run")) == 2


def test_a_window_that_runs_out_is_closed_by_the_worker(engine):
    use(engine, confirmed_rule(trigger=sequence(within=1)), test_mode=True)
    engine.arm()
    engine.armed_at -= 10
    engine.observe_event(event(at=time.monotonic() - 3))
    deadline = time.monotonic() + 3
    while not log(engine, "expired") and time.monotonic() < deadline:
        time.sleep(0.02)
    assert log(engine, "expired") == [
        "No person on legacy-primary within 1 s of zero-entrance.pir; nothing was done."
    ]
    assert rule_state(engine, "entrance-confirmed")["state"] == "watching"


def test_missing_frames_drop_the_motion_and_do_not_count_as_absence(engine):
    use(engine, confirmed_rule(trigger=sequence(absence=1)), test_mode=True)
    engine.arm()
    now = time.monotonic()
    engine.observe_event(event(at=now))
    engine.observe(PERSON, now + 0.01)
    engine.observe(PERSON, now + 0.02)
    assert engine.states["entrance-confirmed"].latched
    # Five seconds without a frame: not an absence, and the latch stays.
    engine.observe([], now + 5)
    engine.observe_event(event(at=now + 5.01))
    assert engine.states["entrance-confirmed"].latched
    assert engine.states["entrance-confirmed"].candidate is None
    assert log(engine, "skipped")


def test_a_camera_gap_drops_a_waiting_motion(engine):
    use(engine, confirmed_rule(), test_mode=True)
    engine.arm()
    now = time.monotonic()
    engine.observe([], now)
    engine.observe_event(event(at=now + 0.01))
    engine.observe(PERSON, now + 5)
    assert engine.states["entrance-confirmed"].candidate is None
    assert log(engine, "cancelled") == [
        "The waiting sequence was dropped: legacy-primary missed frames."
    ]


def test_a_simulated_sequence_changes_nothing(engine):
    use(engine, confirmed_rule())
    result = engine.simulate(
        confirmed_rule(),
        [
            {"at": 0, "value": True},
            {"at": 1, "detections": [{"label": "person", "confidence": 0.9, "box": [0, 0, 1, 1]}]},
            {"at": 2, "detections": [{"label": "person", "confidence": 0.9, "box": [0, 0, 1, 1]}]},
        ],
    )
    assert [step["fired"] for step in result["steps"]] == [False, False, True]
    assert result["steps"][2]["would_run"][0] == "Photo from the triggering camera"
    assert result["executed"] is False
    assert engine.states == {} and not engine.armed
    engine.video.set_sentry.assert_not_called()


# -- fault policies ----------------------------------------------------------------------


def pir_photo():
    return RuleV2(
        id="pir-photo",
        name="PIR photo",
        trigger=SensorEventTrigger(source_id=PIR, kind="motion.pir"),
        actions=[PhotoAction()],
    )


def person():
    return RuleV2(
        id="person", name="Person", trigger=VisionTrigger(object="person"), actions=[PhotoAction()]
    )


def detector_fails(engine):
    engine.video.status.return_value = {
        "capture_running": True,
        "error": None,
        "detection": {"enabled": False, "error": "The model stopped."},
    }
    engine.fault("The model stopped.")


def wait_for(check, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_global_policy_stops_everything_when_the_detector_fails(engine):
    use(engine, person(), confirmed_rule(), pir_photo(), fault_policy="global", test_mode=True)
    engine.arm()
    assert engine.status_v2()["state"] == "protected"
    detector_fails(engine)
    assert not engine.armed
    assert engine.status_v2()["state"] == "fault"
    assert engine.status_v2()["fault_policy"] == "global"


def test_isolated_policy_pauses_only_what_looks_for_objects(engine):
    use(engine, person(), confirmed_rule(), pir_photo(), fault_policy="isolated", test_mode=True)
    engine.arm()
    engine.video.set_sentry.reset_mock()
    detector_fails(engine)
    assert engine.armed  # the callback leaves the decision to the health check
    assert wait_for(lambda: set(engine.suspended) == {"person", "entrance-confirmed"})
    status = engine.status_v2()
    assert status["armed"] and status["state"] == "degraded"
    assert rule_state(engine, "person")["state"] == "paused"
    assert rule_state(engine, "person")["reason"] == "The model stopped."
    assert rule_state(engine, "pir-photo")["state"] == "watching"
    # The camera stays on for the photo, without the detector.
    assert engine.plan.camera and not engine.plan.detector
    assert engine.video.set_sentry.call_args_list[-1].kwargs == {"detect": False}
    engine.observe_event(event())
    assert log(engine, "would_run") == ["Photo"]
    assert log(engine, "waiting") == []


def test_a_camera_that_stops_pauses_the_photo_rule_too(engine):
    use(engine, person(), pir_photo(), fault_policy="isolated", test_mode=True)
    engine.arm()
    engine.video.status.return_value = {
        "capture_running": False,
        "error": "The camera was unplugged.",
        "detection": {"enabled": True, "error": None},
    }
    assert wait_for(lambda: not engine.armed)
    assert engine.status_v2()["state"] == "fault"
    assert "Every armed rule depends on something that failed." in log(engine, "fault")


def test_a_sensor_rule_with_nothing_to_look_at_survives_a_detector_fault(engine):
    use(
        engine,
        person(),
        RuleV2(
            id="pir-hello",
            name="PIR hello",
            trigger=SensorEventTrigger(source_id=PIR, kind="motion.pir"),
            actions=[TTSAction(text="Hello")],
        ),
        fault_policy="isolated",
        test_mode=True,
    )
    engine.arm()
    detector_fails(engine)
    assert wait_for(lambda: "person" in engine.suspended)
    assert not engine.plan.camera
    assert engine.video.set_sentry.call_args_list[-1].args == (False,)
    assert engine.armed


def test_a_node_going_offline_pauses_the_rules_that_listen_to_it(engine):
    use(engine, pir_photo(), person(), fault_policy="isolated", test_mode=True)
    seen = {"zero-entrance": "live"}
    engine.freshness = seen.get
    engine.arm()
    time.sleep(0.2)
    assert not engine.suspended
    seen["zero-entrance"] = "stale"  # late, not gone: nothing pauses yet
    time.sleep(0.2)
    assert not engine.suspended
    seen["zero-entrance"] = "offline"
    assert wait_for(lambda: "pir-photo" in engine.suspended)
    assert engine.suspended["pir-photo"] == "zero-entrance is offline."
    assert engine.armed and "person" not in engine.suspended


def test_a_sensor_moved_to_another_zone_since_arming_opens_nothing():
    run = Run(trigger=sequence(same_zone=True))
    run.motion(0, zone="garden")
    assert run.kinds() == ["skipped"] and run.state.candidate is None
    run.motion(1)
    assert run.kinds()[-1] == "waiting"

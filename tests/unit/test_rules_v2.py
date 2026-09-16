"""The second version of the rules: sensor triggers, planning, context and migration."""

import json
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentry_mode.config import Settings
from sentry_mode.satellites.ingress import NormalizedEvent
from sentry_mode.sentry.config import (
    HealthEventTrigger,
    PhotoAction,
    Rule,
    RuleV2,
    SensorEventTrigger,
    SentryConfig,
    SentryConfigV2,
    ThresholdTrigger,
    TTSAction,
    VisionTrigger,
    WaitAction,
)
from sentry_mode.sentry.context import ActionContext
from sentry_mode.sentry.engine import Job, Sentry
from sentry_mode.sentry.migration import (
    SchemaUpgradeRequired,
    assign_ids,
    read_document,
    to_v1,
    to_v2,
    write_document,
)
from sentry_mode.sentry.resources import ResourcePlanner
from sentry_mode.sentry.triggers import RuleState
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures/satellites/v1"
PIR = "zero-entrance.pir"
TEMPERATURE = "zero-entrance.temperature"


def satellite(source_id, kind=SourceKind.SENSOR, state=SourceState.READY):
    return SourceRecord(
        ref=SourceRef.parse(source_id),
        kind=kind,
        display_name=source_id,
        zone="entrance",
        origin="satellite",
        state=state,
    )


@pytest.fixture
def engine(tmp_path):
    video = MagicMock()
    video.status.return_value = {
        "capture_running": True,
        "error": None,
        "detection": {"enabled": True, "error": None},
    }
    settings = Settings(
        sentry_state_file=tmp_path / "sentry.json",
        captures_directory=tmp_path / "captures",
        sounds_directory=tmp_path / "sounds",
    )
    sentry = Sentry(settings, video, threading.Lock())
    sentry.sources.register(satellite(PIR))
    sentry.sources.register(satellite(TEMPERATURE))
    sentry.sources.register(satellite("zero-entrance.camera", SourceKind.CAMERA))
    sentry.planner = ResourcePlanner(sentry.sources, satellites_enabled=True)
    try:
        yield sentry
    finally:
        sentry.disarm()


def pir_rule(actions=None, rule_id="pir-hello", **fields):
    return RuleV2(
        id=rule_id,
        name=fields.pop("name", "PIR hello"),
        trigger=SensorEventTrigger(source_id=PIR, kind="motion.pir"),
        actions=actions or [TTSAction(text="Hello")],
        cooldown_seconds=fields.pop("cooldown_seconds", 0),
        **fields,
    )


def vision_rule():
    return RuleV2(
        id="person",
        name="Person",
        trigger=VisionTrigger(object="person"),
        actions=[TTSAction(text="Hi")],
    )


def use(engine, *rules, **settings):
    config = SentryConfigV2(rules=list(rules), **settings)
    engine.update_v2(config, engine.revision)


def event(value, *, eligible=True, classification="live", quality="valid", at=None, source=PIR):
    ref = SourceRef.parse(source)
    return NormalizedEvent(
        ref=ref,
        event_id=str(uuid.uuid4()),
        boot_id=str(uuid.uuid4()),
        sequence=1,
        kind="motion.pir" if source == PIR else "climate.temperature",
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
        classification=classification,
        eligible=eligible,
        reason=None if eligible else "initial_state",
    )


def events(engine, kind):
    return [e for e in engine.status()["events"] if e["kind"] == kind]


# -- sensor rules ------------------------------------------------------------------------


def test_a_pir_rule_without_camera_actions_leaves_the_camera_alone(engine):
    use(engine, pir_rule(), test_mode=True)
    engine.arm()
    engine.video.set_sentry.assert_not_called()
    assert engine.plan.camera is False and engine.plan.detector is False
    engine.observe_event(event(True))
    assert [e["message"] for e in events(engine, "would_run")] == ["TTS: Hello"]


def test_a_pir_rule_with_a_photo_keeps_the_camera_open_without_the_detector(engine):
    use(engine, pir_rule([PhotoAction()]), test_mode=True)
    status = engine.arm()
    engine.video.set_sentry.assert_called_once_with(True, 2, 0.7, detect=False)
    assert status["armed"] is True
    assert any("detector is not loaded" in e["message"] for e in events(engine, "configured"))


def test_the_opening_snapshot_and_falling_edges_do_not_fire(engine):
    use(engine, pir_rule(), test_mode=True)
    engine.arm()
    engine.observe_event(event(True, eligible=False, classification="initial"))
    engine.observe_event(event(False))
    engine.observe_event(event(True, quality="degraded"))
    engine.observe_event(event("yes"))
    assert not events(engine, "triggered")


def test_an_event_older_than_the_arming_or_too_late_is_ignored(engine):
    use(engine, pir_rule(), test_mode=True, action_ttl_seconds=5)
    before = time.monotonic()
    engine.arm()
    engine.observe_event(event(True, at=before - 1))
    assert not events(engine, "triggered")
    late = event(True)
    with patch("sentry_mode.sentry.engine.time.monotonic", return_value=time.monotonic() + 30):
        engine.observe_event(late)
    assert not events(engine, "triggered")
    assert events(engine, "expired")


def test_cooldown_applies_to_sensor_rules(engine):
    use(engine, pir_rule(cooldown_seconds=60), test_mode=True)
    engine.arm()
    engine.observe_event(event(True))
    engine.observe_event(event(True))
    assert len(events(engine, "triggered")) == 1


def test_an_event_does_nothing_while_disarmed(engine):
    use(engine, pir_rule(), test_mode=True)
    engine.observe_event(event(True))
    assert not events(engine, "triggered")


# -- thresholds --------------------------------------------------------------------------


def run(engine, trigger, readings):
    rule = RuleV2(
        id="hot", name="Hot", trigger=trigger, cooldown_seconds=0, actions=[TTSAction(text="Hot")]
    )
    samples = []
    for at, value, *quality in readings:
        sample = {"at": at, "value": value}
        if quality:
            sample["quality"] = quality[0]
        samples.append(sample)
    result = engine.simulate(rule, samples)
    return [step["at"] for step in result["steps"] if step["fired"]]


def test_a_threshold_waits_its_duration_and_releases_only_past_the_hysteresis(engine):
    trigger = ThresholdTrigger(
        source_id=TEMPERATURE, kind="climate.temperature", above=30, hysteresis=2, for_seconds=10
    )
    readings = [
        (0, 25),
        (1, 31),  # past the limit: the wait starts
        (5, None, "unavailable"),  # unknown time past the limit: the wait starts again
        (6, 31),
        (15, 31),
        (16, 32),  # ten seconds since 6
        (17, 29),  # back under, but not by the hysteresis
        (18, 31),
        (40, 31),
        (41, 27),  # released
        (42, 31),
        (52, 31),
    ]
    assert run(engine, trigger, readings) == [16, 52]


def test_a_value_already_past_the_limit_when_watching_starts_is_not_a_crossing(engine):
    trigger = ThresholdTrigger(source_id=TEMPERATURE, kind="climate.temperature", above=30)
    assert run(engine, trigger, [(0, 40), (1, 41), (2, 45)]) == []
    assert run(engine, trigger, [(0, 40), (1, 20), (2, 45)]) == [2]


def test_a_missing_reading_is_not_a_zero(engine):
    trigger = ThresholdTrigger(source_id=TEMPERATURE, kind="climate.temperature", below=5)
    readings = [(0, 20), (1, None, "unavailable"), (2, 0, "degraded"), (3, "cold")]
    assert run(engine, trigger, readings) == []
    assert run(engine, trigger, [(0, 20), (1, 0)]) == [1]


def test_a_simulation_leaves_the_armed_engine_alone(engine):
    use(engine, pir_rule(), vision_rule(), test_mode=True)
    engine.arm()
    before = {key: vars(state).copy() for key, state in engine.states.items()}
    result = engine.simulate(pir_rule(), [{"at": 0, "value": True}, {"at": 1, "value": False}])
    assert [step["fired"] for step in result["steps"]] == [True, False]
    assert result["executed"] is False
    assert {key: vars(state) for key, state in engine.states.items()} == before
    assert not events(engine, "triggered")


def test_a_simulation_of_a_vision_rule_uses_the_same_confirmation(engine):
    rule = vision_rule()
    person = {"label": "person", "confidence": 0.9, "box": [0.2, 0.2, 0.8, 0.8]}
    samples = [{"at": t, "detections": [person]} for t in (0, 0.5, 1, 1.5)]
    steps = engine.simulate(rule, samples)["steps"]
    assert [step["fired"] for step in steps] == [False, False, True, False]


def test_a_trigger_that_cannot_be_watched_cannot_be_simulated(engine):
    rule = RuleV2(
        id="offline",
        name="Offline",
        trigger=HealthEventTrigger(node_id="zero-entrance"),
        actions=[TTSAction(text="Gone")],
    )
    with pytest.raises(ValueError, match="cannot be simulated"):
        engine.simulate(rule, [{"at": 0}])


# -- the queue and the context -----------------------------------------------------------


def test_a_sequence_is_queued_whole_or_not_at_all(engine):
    rule = pir_rule([TTSAction(text="one"), WaitAction(seconds=1), TTSAction(text="two")])
    use(engine, rule)
    engine.armed = True
    engine.states = {rule.id: RuleState()}
    for _ in range(engine.jobs.maxsize - 2):
        engine.jobs.put_nowait(Job("other", [TTSAction(text="x")], time.monotonic()))
    with engine.guard:
        engine._fire(engine.config.rules[0], time.monotonic(), ("e1",), None)
    assert engine.jobs.qsize() == engine.jobs.maxsize - 2
    assert "whole sequence of 3 steps" in events(engine, "skipped")[-1]["message"]
    engine.armed = False


def test_queued_jobs_carry_their_context(engine):
    rule = pir_rule([PhotoAction()])
    use(engine, rule)
    engine.armed, engine.arm_epoch = True, 4
    engine.states = {rule.id: RuleState()}
    with engine.guard:
        engine._fire(engine.config.rules[0], time.monotonic(), ("event-1",), "entrance")
    context = engine.jobs.get_nowait().context
    assert context.rule_id == "pir-hello"
    assert context.arm_epoch == 4
    assert context.origin == ("event-1",)
    assert context.zone == "entrance"
    assert context.sources == ("legacy-primary",)
    assert context.rule_revision == engine.revision
    engine.armed = False


def test_an_action_from_an_earlier_arming_is_dropped(engine):
    engine.cancelled.clear()
    engine.arm_epoch = 2
    context = ActionContext.new(
        rule_id="r",
        rule_name="R",
        rule_revision=1,
        arm_epoch=1,
        zone=None,
        origin=("e",),
        sources=(),
    )
    with patch("sentry_mode.sentry.engine.speak") as speech:
        engine._run(Job("R", [TTSAction(text="late")], time.monotonic(), 0, context))
    speech.assert_not_called()
    assert events(engine, "cancelled")[-1]["message"] == "Action belonged to an earlier arming."
    engine.cancelled.set()


def test_disarming_moves_the_epoch_on(engine):
    use(engine, pir_rule(), test_mode=True)
    engine.arm()
    armed = engine.arm_epoch
    engine.disarm()
    assert engine.arm_epoch == armed + 1
    engine.arm()
    assert engine.arm_epoch == armed + 2


# -- planning and faults -----------------------------------------------------------------


@pytest.mark.parametrize(
    "rule,problem",
    [
        (
            RuleV2(
                id="x",
                name="X",
                trigger=SensorEventTrigger(source_id="zero-garden.pir", kind="motion.pir"),
                actions=[TTSAction(text="x")],
            ),
            "not a known source",
        ),
        (
            RuleV2(
                id="x",
                name="X",
                trigger=SensorEventTrigger(source_id="zero-entrance.camera", kind="motion.pir"),
                actions=[TTSAction(text="x")],
            ),
            "is a camera, not a sensor",
        ),
        (
            RuleV2(
                id="x",
                name="X",
                trigger=VisionTrigger(source_id="zero-entrance.camera", object="person"),
                actions=[TTSAction(text="x")],
            ),
            "satellite camera is not available yet",
        ),
        (
            RuleV2(
                id="x",
                name="X",
                trigger=HealthEventTrigger(node_id="zero-entrance"),
                actions=[TTSAction(text="x")],
            ),
            "health event triggers are not available yet",
        ),
        (
            pir_rule([PhotoAction(source_id="zero-entrance.camera")]),
            "recording from a satellite camera is not available yet",
        ),
    ],
)
def test_arming_names_what_is_missing(engine, rule, problem):
    use(engine, rule, test_mode=True)
    with pytest.raises(ValueError, match=problem):
        engine.arm()
    assert engine.armed is False
    engine.video.set_sentry.assert_not_called()


def test_a_disabled_source_or_switched_off_satellites_refuse_to_arm(engine):
    use(engine, pir_rule(), test_mode=True)
    engine.sources.set_state(PIR, SourceState.DISABLED)
    with pytest.raises(ValueError, match="is disabled"):
        engine.arm()
    engine.sources.set_state(PIR, SourceState.READY)
    engine.planner = ResourcePlanner(engine.sources, satellites_enabled=False)
    with pytest.raises(ValueError, match="satellites are switched off"):
        engine.arm()


def test_a_disabled_camera_is_a_planning_problem(engine):
    use(engine, vision_rule(), test_mode=True)
    engine.sources.set_state("legacy-primary", SourceState.DISABLED)
    with pytest.raises(ValueError, match="legacy-primary is disabled"):
        engine.arm()


def test_isolated_faults_pause_only_the_rules_that_need_the_source(engine):
    use(engine, pir_rule(), vision_rule(), test_mode=True, fault_policy="isolated")
    engine.arm()
    engine.sources.set_state(PIR, SourceState.FAILED)
    time.sleep(0.3)
    assert engine.armed is True
    assert set(engine.suspended) == {"pir-hello"}
    engine.observe_event(event(True))
    assert not events(engine, "triggered")


def test_isolated_faults_disarm_when_nothing_is_left(engine):
    use(engine, pir_rule(), test_mode=True, fault_policy="isolated")
    engine.arm()
    engine.sources.set_state(PIR, SourceState.FAILED)
    time.sleep(0.3)
    assert engine.armed is False
    assert "no longer available" in engine.error


def test_a_global_fault_disarms_everything(engine):
    use(engine, pir_rule(), vision_rule(), test_mode=True, fault_policy="global")
    engine.arm()
    engine.sources.set_state(PIR, SourceState.FAILED)
    time.sleep(0.3)
    assert engine.armed is False


def test_an_isolated_camera_fault_releases_the_camera_for_sensor_rules(engine):
    use(engine, pir_rule(), vision_rule(), test_mode=True, fault_policy="isolated")
    engine.arm()
    engine.video.status.return_value = {
        "capture_running": False,
        "error": "Camera unplugged",
        "detection": {"enabled": True, "error": None},
    }
    time.sleep(0.3)
    assert engine.armed is True
    assert set(engine.suspended) == {"person"}
    assert engine.video.set_sentry.call_args_list[-1].args == (False,)
    engine.observe_event(event(True))
    assert len(events(engine, "triggered")) == 1


# -- the two versions of the document ----------------------------------------------------


def legacy():
    data = json.loads((FIXTURES / "sentry-v1-state.json").read_text())
    return SentryConfig.model_validate(data["config"])


def test_conversion_to_the_second_version_and_back_loses_nothing():
    config = legacy()
    converted = to_v2(config)
    assert converted.fault_policy == "global"
    assert [rule.id for rule in converted.rules] == ["person-at-entrance", "vehicle-at-gate"]
    assert to_v1(converted) == config
    assert to_v2(to_v1(converted), previous=converted) == converted


def test_rule_ids_survive_renames_of_other_rules_and_never_collide():
    assert assign_ids(["Front door", "Front-door", "front door"]) == [
        "front-door",
        "front-door-2",
        "front-door-3",
    ]
    assert assign_ids(["New", "Old"], {"Old": "kept-id"}) == ["new", "kept-id"]
    assert assign_ids(["!!!"]) == ["rule"]


def test_a_first_version_editor_keeps_the_ids_and_the_policy(engine):
    use(engine, vision_rule(), fault_policy="isolated")
    engine.update(to_v1(engine.config), engine.revision)
    assert engine.config.rules[0].id == "person"
    assert engine.config.fault_policy == "isolated"


def test_rules_the_first_version_cannot_hold_are_refused_to_it(engine):
    use(engine, pir_rule())
    with pytest.raises(SchemaUpgradeRequired) as refused:
        engine.configuration()
    assert refused.value.code == "schema_upgrade_required"
    assert "PIR hello is set off by sensor event" in refused.value.reasons
    with pytest.raises(SchemaUpgradeRequired):
        engine.update(SentryConfig(), engine.revision)
    assert engine.config.rules[0].id == "pir-hello"
    assert engine.configuration_v2()["schema_version"] == 2


def test_a_node_without_a_rules_file_writes_the_second_version_when_it_must(engine):
    path = engine.settings.sentry_state_file
    use(engine, pir_rule())
    assert json.loads(path.read_text())["schema_version"] == 2
    assert engine.schema_version == 2
    use(engine, vision_rule())
    assert json.loads(path.read_text())["schema_version"] == 2  # never back down


def test_a_file_the_node_wrote_in_the_first_version_needs_the_script(engine):
    # Once written, a first-version file is one the previous release may be reading.
    path = engine.settings.sentry_state_file
    use(engine, vision_rule())
    assert "schema_version" not in json.loads(path.read_text())
    with pytest.raises(SchemaUpgradeRequired, match="migrate_satellites"):
        use(engine, pir_rule())


def test_a_first_version_file_stays_in_the_first_version(tmp_path):
    path = tmp_path / "sentry.json"
    shutil.copy(FIXTURES / "sentry-v1-state.json", path)
    video = MagicMock()
    engine = Sentry(Settings(sentry_state_file=path), video, threading.Lock())
    engine.update(to_v1(engine.config), engine.revision)
    saved = json.loads(path.read_text())
    assert "schema_version" not in saved
    assert SentryConfig.model_validate(saved["config"])
    engine.sources.register(satellite(PIR))
    with pytest.raises(SchemaUpgradeRequired, match="migrate_satellites"):
        engine.update_v2(SentryConfigV2(rules=[pir_rule()]), engine.revision)
    assert "schema_version" not in json.loads(path.read_text())
    assert engine.schema_version == 1


def test_the_saved_document_keeps_its_secret_and_version():
    token = "123456:" + "a" * 35
    config = SentryConfigV2(rules=[pir_rule()], telegram={"bot_token": token})
    body = write_document(config, 7, 2)
    assert body["config"]["telegram"]["bot_token"] == token
    document = read_document(json.loads(json.dumps(body)))
    assert document.config == config and document.revision == 7
    with pytest.raises(ValueError, match="newer"):
        read_document({**body, "schema_version": 3})


def test_a_first_version_rule_can_still_be_tested(engine):
    rule = Rule(name="Draft", object="person", actions=[TTSAction(text="Hi")])
    with patch("sentry_mode.sentry.engine.speak"):
        assert engine.test(rule) == {"message": "Running the actions on the node."}
        engine.thread.join(timeout=3)
    assert events(engine, "tested")


def test_testing_a_rule_that_records_from_a_satellite_is_refused(engine):
    rule = pir_rule([PhotoAction(source_id="zero-entrance.camera")])
    with pytest.raises(ValueError, match="satellite camera is not available yet"):
        engine.test(rule)


def test_the_camera_can_stay_open_for_sentry_without_the_detector():
    import numpy as np

    from sentry_mode.config import CameraConfig
    from sentry_mode.vision.stream import VideoStream

    lock = threading.Lock()
    stream = VideoStream(CameraConfig(), lock)
    with (
        patch("sentry_mode.vision.stream.Camera") as adapter,
        patch("sentry_mode.vision.detection.ObjectDetector") as detector,
    ):
        camera = adapter.return_value.__enter__.return_value
        camera.capture_frame.return_value = np.zeros((120, 160, 3), np.uint8)
        try:
            state = stream.set_sentry(True, 2, 0.7, detect=False)
            assert state["capture_running"] and not state["running"]
            assert state["detection"]["enabled"] is False
            time.sleep(0.2)
            detector.assert_not_called()
            stream.set_sentry(False)
            assert not stream.status()["capture_running"] and not lock.locked()
        finally:
            stream.close()

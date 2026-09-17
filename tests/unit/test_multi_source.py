"""The primary camera and two synthetic ones, armed together on one shared model."""

import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from sentry_mode.config import CameraConfig, DetectionConfig, Settings
from sentry_mode.satellites.ingress import NormalizedEvent
from sentry_mode.sentry import engine as engine_module
from sentry_mode.sentry.config import (
    PhotoAction,
    RuleV2,
    SensorEventTrigger,
    SentryConfigV2,
    TTSAction,
    VisionTrigger,
)
from sentry_mode.sentry.engine import Sentry
from sentry_mode.sentry.resources import ResourcePlanner
from sentry_mode.sources.legacy import legacy_sources
from sentry_mode.sources.manager import SourceManager
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.detection import Detection
from sentry_mode.vision.scheduler import InferenceScheduler
from sentry_mode.vision.stream import VideoStream
from sentry_mode.vision.synthetic import SyntheticCamera

HERE = Path(__file__).parent
PIR = "zero-entrance.pir"


def image(label):
    frame = np.zeros((48, 64, 3), np.uint8)
    frame[:, :, 0] = label
    return frame


def until(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


class Broken:
    def __init__(self):
        self.after = None

    def __call__(self, n):
        if self.after is not None and n >= self.after:
            raise RuntimeError("synthetic-b lost its frames")
        return image(2)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    settings = Settings(
        sentry_state_file=tmp_path / "sentry.json",
        captures_directory=tmp_path / "captures",
        sounds_directory=tmp_path / "sounds",
        detection=DetectionConfig(budget_fps=10),
    )
    shared = InferenceScheduler(settings.detection, factory="fake_models:Painted")
    video = VideoStream(CameraConfig(), threading.Lock(), settings.detection, scheduler=shared)
    registry = SourceRegistry()
    for record in legacy_sources(settings):
        registry.register(record)
    registry.register(
        SourceRecord(
            ref=SourceRef.parse(PIR),
            kind=SourceKind.SENSOR,
            display_name="PIR",
            origin="satellite",
            state=SourceState.READY,
        )
    )
    cameras = SourceManager(registry)
    cameras.add(video, register=False)
    broken = Broken()
    cameras.add(
        SyntheticCamera("synthetic-a", shared, settings.detection, frames=lambda n: image(1))
    )
    cameras.add(SyntheticCamera("synthetic-b", shared, settings.detection, frames=broken))
    sentry = Sentry(settings, video, threading.Lock(), registry, cameras)
    sentry.planner = ResourcePlanner(
        registry, satellites_enabled=True, cameras=engine_module._Others(cameras)
    )
    with patch("sentry_mode.vision.stream.Camera") as adapter:
        adapter.return_value.__enter__.return_value.capture_frame.return_value = image(1)
        try:
            yield sentry, shared, cameras, broken
        finally:
            sentry.disarm()
            cameras.close()
            video.close()
            shared.close()
            sys.modules.pop("fake_models", None)


def looking(rule_id, source_id, thing):
    return RuleV2(
        id=rule_id,
        name=rule_id,
        trigger=VisionTrigger(
            source_id=source_id, object=thing, consecutive_detections=2, min_confidence=0.5
        ),
        actions=[TTSAction(text=f"{thing} at {source_id}")],
    )


def use(sentry, *rules, **settings):
    sentry.update_v2(SentryConfigV2(rules=list(rules), test_mode=True, **settings), sentry.revision)


def fired(sentry):
    return {e["rule"] for e in sentry.status()["events"] if e["kind"] == "triggered"}


def test_three_cameras_share_one_model_and_each_rule_hears_its_own(hub):
    sentry, shared, cameras, _ = hub
    import fake_models

    fake_models.Painted.instances = 0
    use(
        sentry,
        looking("front", "legacy-primary", "person"),
        looking("yard", "synthetic-a", "person"),
        looking("garden", "synthetic-b", "dog"),
        looking("wrong-camera", "synthetic-a", "dog"),
    )
    sentry.arm()
    assert until(lambda: fired(sentry) >= {"front", "yard", "garden"})
    assert "wrong-camera" not in fired(sentry)
    assert fake_models.Painted.instances == 1
    sources = shared.status()["sources"]
    assert {"legacy-primary", "synthetic-a", "synthetic-b"} <= set(sources)
    assert all(sources[name]["inferences"] for name in sources)
    sentry.disarm()
    for name in ("synthetic-a", "synthetic-b"):
        assert until(lambda name=name: not cameras.camera(name).status()["capture_running"])
    assert not sentry.video.status()["capture_running"]
    assert all(s["requested_fps"] == 0 for s in shared.status()["sources"].values())


def test_rules_that_only_listen_to_sensors_never_ask_for_inference(hub):
    sentry, shared, cameras, _ = hub
    use(
        sentry,
        RuleV2(
            id="pir",
            name="pir",
            trigger=SensorEventTrigger(source_id=PIR, kind="motion.pir"),
            actions=[TTSAction(text="hello")],
        ),
    )
    sentry.arm()
    time.sleep(0.2)
    assert shared.loads == 0 and shared.thread is None
    assert not any(camera.status()["capture_running"] for camera in cameras)


def test_one_cameras_gap_does_not_reset_another(hub):
    sentry, _, cameras, _ = hub
    use(sentry, looking("yard", "synthetic-a", "person"), looking("garden", "synthetic-b", "dog"))
    sentry.arm()
    for camera in cameras:
        camera.detection.on_result = None  # only the samples below reach the engine
    person = [Detection("person", 0.9, (0.1, 0.1, 0.5, 0.5))]
    dog = [Detection("dog", 0.9, (0.1, 0.1, 0.5, 0.5))]
    time.sleep(0.3)  # results already on their way have landed
    start = time.monotonic() + 1  # after anything the cameras delivered
    sentry.observe(dog, start + 0.1, "synthetic-b")
    sentry.observe(person, start, "synthetic-a")
    sentry.observe(person, start + 2.5, "synthetic-a")  # a gap on A: A starts over
    sentry.observe(person, start + 2.5, "synthetic-a")  # the same frame again counts for nothing
    assert sentry.states["yard"].hits == 1
    assert sentry.states["garden"].hits == 1  # B kept its sample
    sentry.observe(dog, start + 0.2, "synthetic-b")
    assert "garden" in fired(sentry) and "yard" not in fired(sentry)


def test_a_failing_camera_pauses_only_its_own_rules(hub):
    sentry, _, cameras, broken = hub
    use(sentry, looking("yard", "synthetic-a", "person"), looking("garden", "synthetic-b", "dog"))
    sentry.arm()
    broken.after = cameras.camera("synthetic-b").captured + 1
    assert until(lambda: "garden" in sentry.suspended)
    assert "lost its frames" in sentry.suspended["garden"]
    assert sentry.status()["armed"] and "yard" not in sentry.suspended
    assert "synthetic-b" not in sentry.watching
    assert until(lambda: "yard" in fired(sentry))


def test_a_motion_sensor_makes_the_watching_cameras_look_harder(hub):
    sentry, shared, _, _ = hub
    use(sentry, looking("yard", "synthetic-a", "person"))
    sentry.arm()
    assert until(lambda: shared.status("synthetic-a")["requested_fps"] == 2)
    sentry.observe_event(
        NormalizedEvent(
            ref=SourceRef.parse(PIR),
            event_id=str(uuid.uuid4()),
            boot_id=str(uuid.uuid4()),
            sequence=1,
            kind="motion.pir",
            value=True,
            unit=None,
            quality="valid",
            clock_status="synced",
            zone="entrance",
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
    )
    assert shared.status("synthetic-a")["boosted"]
    assert shared.status("synthetic-a")["requested_fps"] == 2 * engine_module.BOOST_FACTOR


def test_the_planner_knows_which_cameras_it_can_watch(hub):
    sentry, _, _, _ = hub
    photo = looking("yard", "synthetic-a", "person").model_copy(
        update={"actions": [PhotoAction(source_id="synthetic-a")]}
    )
    plan = sentry.planner.plan([photo])
    assert [p.message for p in plan.problems] == [
        "photos and videos come only from legacy-primary for now"
    ]
    plan = sentry.planner.plan([looking("yard", "synthetic-a", "person")])
    assert plan.ok and plan.vision == {"synthetic-a": 0.5} and not plan.camera


# -- a camera on a satellite, through the same leases -------------------------------------

REMOTE = "zero-gate.camera-1"


class Painting:
    """A decoder stand-in: every chunk fed in comes out as one painted frame."""

    def __init__(self, width, height, on_frame, **options):
        self.on_frame = on_frame
        self.frames = self.bytes_in = self.queued_bytes = 0
        self.error = None

    def start(self):
        pass

    def feed(self, data):
        self.frames += 1
        self.on_frame(image(data[0]))

    def close(self):
        pass


class Node:
    """A satellite that answers video_start by connecting and sending frames of one label."""

    def __init__(self, label=2):
        self.label = label
        self.reachable = True
        self.stopped = threading.Event()
        self.asked = 0

    def start_video(self, node_id, source_id, connect, on_end):
        if not self.reachable:
            raise BlockingIOError(f"{node_id} is offline")
        self.asked += 1
        self.stopped.clear()

        def send():
            decoder = connect()
            while not self.stopped.wait(0.1):
                decoder.feed(bytes([self.label]))

        threading.Thread(target=send, daemon=True).start()
        return f"stream-{self.asked}"

    def renew_video(self, node_id, stream_id):
        return True

    def stop_video(self, node_id, stream_id):
        self.stopped.set()


@pytest.fixture
def remote_hub(hub):
    from sentry_mode.satellites.config import MediaConfig
    from sentry_mode.vision.remote import RemoteCamera

    sentry, shared, cameras, broken = hub
    sentry.sources.register(
        SourceRecord(
            ref=SourceRef.parse(REMOTE),
            kind=SourceKind.CAMERA,
            display_name="Gate",
            origin="satellite",
            state=SourceState.READY,
        )
    )
    node = Node()
    camera = RemoteCamera(
        "zero-gate",
        "camera-1",
        {"width": 64, "height": 48},
        link=node,
        scheduler=shared,
        detection=DetectionConfig(budget_fps=10),
        media=MediaConfig(connect_timeout_seconds=2),
        decoder=Painting,
    )
    cameras.add(camera, register=False)
    yield sentry, shared, camera, node


def test_a_satellite_camera_is_watched_like_any_other(remote_hub):
    sentry, shared, camera, node = remote_hub
    use(sentry, looking("front", "legacy-primary", "person"), looking("gate", REMOTE, "dog"))
    assert node.asked == 0
    sentry.arm()
    assert until(lambda: fired(sentry) >= {"front", "gate"})
    assert shared.status(REMOTE)["inferences"] > 0
    assert camera.status()["state"] == "live"
    started = time.monotonic()
    sentry.disarm()
    assert time.monotonic() - started < 5
    assert node.stopped.is_set()
    assert camera.status()["state"] == "idle"


def test_an_unreachable_satellite_camera_pauses_only_its_rule(remote_hub):
    sentry, _, camera, node = remote_hub
    node.reachable = False
    use(sentry, looking("front", "legacy-primary", "person"), looking("gate", REMOTE, "dog"))
    sentry.arm()
    assert until(lambda: "gate" in sentry.suspended)
    assert "offline" in sentry.suspended["gate"]
    assert sentry.status()["armed"]
    assert until(lambda: "front" in fired(sentry))
    started = time.monotonic()
    sentry.disarm()
    assert time.monotonic() - started < 5

"""Evidence from the source a rule asked for, and previews that belong to one page each."""

import json
import os
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
from sentry_mode.core.errors import HardwareError
from sentry_mode.satellites.ingress import NormalizedEvent
from sentry_mode.sentry import engine as engine_module
from sentry_mode.sentry.config import (
    TRIGGER_SOURCE,
    PhotoAction,
    RuleV2,
    SensorEventTrigger,
    SentryConfigV2,
    TTSAction,
    VideoAction,
    VisionTrigger,
)
from sentry_mode.sentry.engine import Sentry
from sentry_mode.sentry.migration import obstacles
from sentry_mode.sentry.resources import ResourcePlanner
from sentry_mode.sources.legacy import legacy_sources
from sentry_mode.sources.manager import SourceManager
from sentry_mode.sources.models import SourceKind, SourceRecord, SourceRef, SourceState
from sentry_mode.sources.registry import SourceRegistry
from sentry_mode.vision.preview import PreviewSessions
from sentry_mode.vision.recording import Captures
from sentry_mode.vision.scheduler import InferenceScheduler
from sentry_mode.vision.stream import VideoStream
from sentry_mode.vision.synthetic import SyntheticCamera

HERE = Path(__file__).parent
PIR = "zero-entrance.pir"


def image(value):
    return np.full((48, 64, 3), value, np.uint8)


def until(check, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


class Frames:
    """A synthetic camera's pictures, which a test can switch off."""

    def __init__(self, value):
        self.value = value
        self.frozen = False
        self.last = None

    def __call__(self, n):
        if self.frozen and self.last is not None:
            time.sleep(0.2)
            raise RuntimeError("the camera stopped sending")
        self.last = image(self.value)
        return self.last


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
            display_name="Entrance PIR",
            origin="satellite",
            zone="entrance",
            state=SourceState.READY,
        )
    )
    cameras = SourceManager(registry)
    cameras.add(video, register=False)
    a, b = Frames(60), Frames(200)
    cameras.add(SyntheticCamera("camera-a", shared, settings.detection, frames=a))
    cameras.add(SyntheticCamera("camera-b", shared, settings.detection, frames=b))
    sentry = Sentry(settings, video, threading.Lock(), registry, cameras)
    sentry.planner = ResourcePlanner(
        registry, satellites_enabled=True, cameras=engine_module._Others(cameras)
    )
    previews = PreviewSessions(cameras, video, registry)
    with patch("sentry_mode.vision.stream.Camera") as adapter:
        adapter.return_value.__enter__.return_value.capture_frame.return_value = image(10)
        adapter.return_value.__enter__.return_value.encode_jpeg.return_value = b"primary-jpeg"
        try:
            yield sentry, cameras, previews, (a, b)
        finally:
            previews.close_all()
            sentry.disarm()
            cameras.close()
            video.close()
            shared.close()
            sys.modules.pop("fake_models", None)


def pir_rule(actions, rule_id="door"):
    return RuleV2(
        id=rule_id,
        name=rule_id,
        trigger=SensorEventTrigger(source_id=PIR, kind="sensor.motion"),
        cooldown_seconds=0,
        actions=actions,
    )


def use(sentry, *rules, **settings):
    sentry.update_v2(SentryConfigV2(rules=list(rules), **settings), sentry.revision)


def motion(zone="entrance"):
    return NormalizedEvent(
        ref=SourceRef.parse(PIR),
        event_id=str(uuid.uuid4()),
        boot_id=str(uuid.uuid4()),
        sequence=1,
        kind="sensor.motion",
        value=True,
        unit=None,
        quality="valid",
        clock_status="synced",
        zone=zone,
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


def events(sentry, kind):
    return [e["message"] for e in sentry.status()["events"] if e["kind"] == kind]


def sidecars(sentry):
    folder = sentry.captures.directory
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


# -- ACC-09: the photo comes from the camera the rule named ---------------------------------


def test_photo_uses_action_context_not_current_preview(hub):
    import cv2

    sentry, cameras, previews, _ = hub
    use(sentry, pir_rule([PhotoAction(source_id="camera-a")]))
    # Somebody is watching camera B; the rule is about camera A.
    watching = previews.open("camera-b")
    assert until(lambda: cameras.camera("camera-b").latest_capture() is not None)
    sentry.arm()
    assert sentry.plan.ready == {"camera-a"}
    assert until(lambda: cameras.camera("camera-a").latest_capture() is not None)
    sentry.observe_event(motion())
    assert until(lambda: events(sentry, "action_finished"))
    [finished] = events(sentry, "action_finished")
    assert finished.endswith("from camera-a")
    [meta] = sidecars(sentry)
    assert meta["source_id"] == "camera-a" and meta["timing"] == "at_trigger"
    assert meta["rule_id"] == "door" and meta["trigger_zone"] == "entrance"
    assert meta["frame_age_seconds"] <= engine_module.FRESH_SECONDS
    assert meta["sequence"] == {"index": 1, "count": 1}
    assert meta["trigger_origin"] and meta["arm_epoch"] == sentry.arm_epoch
    [photo] = sentry.captures.directory.glob("*.jpg")
    pixels = cv2.imread(str(photo))
    assert abs(int(pixels.mean()) - 60) <= 2  # camera A's grey, not camera B's
    [listed] = sentry.captures.listing()["captures"]
    assert listed["evidence"]["source_id"] == "camera-a"
    previews.close(watching["session"])


def test_a_later_photo_is_marked_as_taken_after_the_trigger(hub):
    sentry, _, _, _ = hub
    use(sentry, pir_rule([PhotoAction(source_id="camera-a", count=2, interval_seconds=0.5)]))
    sentry.arm()
    assert until(lambda: sentry.cameras.camera("camera-a").latest_capture() is not None)
    sentry.observe_event(motion())
    assert until(lambda: len(events(sentry, "action_finished")) == 2)
    timings = sorted((m["sequence"]["index"], m["timing"]) for m in sidecars(sentry))
    assert timings == [(1, "at_trigger"), (2, "after_trigger")]
    later = next(m for m in sidecars(sentry) if m["sequence"]["index"] == 2)
    assert later["seconds_after_trigger"] > 0


def test_the_triggering_camera_is_resolved_when_the_rule_fires(hub):
    sentry, _, _, _ = hub
    rule = RuleV2(
        id="yard",
        name="yard",
        trigger=VisionTrigger(source_id="camera-b", object="person"),
        actions=[PhotoAction(source_id=TRIGGER_SOURCE)],
    )
    plan = sentry.planner.plan([rule])
    assert plan.ok and plan.vision == {"camera-b": 0.7} and not plan.ready
    use(sentry, rule, test_mode=True)
    with sentry.guard:
        sentry._fire(sentry.config.rules[0], time.monotonic(), ("vision:camera-b",), None)
    assert events(sentry, "would_run") == ["Photo from the triggering camera"]
    context = sentry._context(sentry.config.rules[0], ("vision:camera-b",))
    assert context.sources == ("camera-b",)
    jobs = sentry._groups(sentry.config.rules[0], time.monotonic(), context)
    assert jobs[0].steps[0].source_id == "camera-b"


def test_a_sensor_rule_has_to_name_the_camera_it_photographs(hub):
    sentry, _, _, _ = hub
    rule = pir_rule([PhotoAction(source_id=TRIGGER_SOURCE)])
    use(sentry, rule)
    with pytest.raises(ValueError, match="photo after a sensor event trigger has to name its"):
        sentry.arm()
    assert obstacles(SentryConfigV2(rules=[rule]))


def test_a_camera_kept_ready_for_photos_is_released_on_disarm(hub):
    sentry, cameras, _, _ = hub
    use(sentry, pir_rule([PhotoAction(source_id="camera-b")]))
    sentry.arm()
    camera = cameras.camera("camera-b")
    assert camera.status()["leases"]["monitoring"] == 1
    assert not camera.status()["detection"]["enabled"]
    sentry.disarm()
    assert camera.status()["leases"]["monitoring"] == 0
    assert until(lambda: not camera.status()["capture_running"])


# -- ACC-10: a missing source is reported, never replaced -----------------------------------


@pytest.mark.parametrize(
    ("policy", "kind", "said"),
    [
        ("fail", "action_failed", "no other camera was used"),
        ("skip", "skipped", "step skipped"),
        ("stop", "cancelled", "the rest of this sequence was dropped"),
    ],
)
def test_missing_media_source_has_no_silent_fallback(hub, policy, kind, said):
    sentry, cameras, _, (_, b) = hub
    use(
        sentry,
        pir_rule(
            [
                PhotoAction(source_id="camera-b", if_unavailable=policy),
                TTSAction(text="after the photo"),
            ]
        ),
    )
    sentry.arm()
    assert until(lambda: cameras.camera("camera-b").latest_capture() is not None)
    b.frozen = True
    assert until(
        lambda: (
            time.monotonic() - cameras.camera("camera-b").latest_capture()[1]
            > engine_module.FRESH_SECONDS
        ),
        timeout=6,
    )
    with patch("sentry_mode.sentry.engine.speak") as speech:
        sentry.observe_event(motion())
        assert until(lambda: any(said in m for m in events(sentry, kind)))
        if policy == "stop":
            assert until(lambda: "An earlier step stopped this sequence." in events(sentry, kind))
            speech.assert_not_called()
        else:
            assert until(lambda: speech.called)
    assert not list(sentry.captures.directory.glob("*.jpg"))
    assert any("camera-b" in m for m in events(sentry, kind))


def test_a_photo_from_a_camera_that_vanished_fails_by_name(hub):
    sentry, cameras, _, _ = hub
    use(sentry, pir_rule([PhotoAction(source_id="camera-a")]), test_mode=False)
    sentry.arm()
    with patch.object(cameras, "camera", return_value=None):
        sentry.observe_event(motion())
        assert until(lambda: events(sentry, "action_failed"))
    assert events(sentry, "action_failed") == [
        "camera-a is not a camera on this hub; no other camera was used."
    ]


# -- ACC-11: sound only from a microphone the rule chose --------------------------------------


def test_remote_video_never_opens_unassigned_local_mic(hub):
    sentry, _, _, _ = hub
    rule = pir_rule([VideoAction(source_id="camera-a", duration_seconds=1)])
    assert rule.actions[0].audio and rule.actions[0].microphone is None
    use(sentry, rule)
    plan = sentry.planner.plan([rule])
    assert plan.ok and any("without sound" in note for note in plan.notes)
    recorded = {}

    def record(frames, seconds, rule, stop_event, size, microphone, meta):
        recorded.update(microphone=microphone, frame=frames(), size=size, meta=meta)
        return "20260101-000000-000-door.mp4", None

    with (
        patch.object(sentry.captures, "record_video", side_effect=record),
        patch.object(Sentry, "_microphone") as microphone,
    ):
        sentry.arm()
        assert until(lambda: sentry.cameras.camera("camera-a").latest_capture() is not None)
        sentry.observe_event(motion())
        assert until(lambda: events(sentry, "action_finished"))
    microphone.assert_not_called()
    assert recorded["microphone"] is None and recorded["size"] is None
    assert int(recorded["frame"].mean()) == 60
    assert recorded["meta"]["source_id"] == "camera-a"
    assert recorded["meta"]["audio_source_id"] is None
    assert events(sentry, "action_finished") == [
        "Video saved: 20260101-000000-000-door.mp4 from camera-a"
        " (no sound: no microphone is chosen for camera-a)"
    ]
    lease_counts = sentry.cameras.camera("camera-a").status()["leases"]
    assert lease_counts["recording"] == 0


def test_the_node_camera_keeps_its_microphone_when_none_is_named():
    legacy = VideoAction()
    assert legacy.microphone == "legacy-microphone"
    assert VideoAction(audio=False).microphone is None
    chosen = VideoAction(source_id="zero-gate.camera-1", audio_source_id="legacy-microphone")
    assert chosen.microphone == "legacy-microphone"
    seen = RuleV2(
        id="front", name="front", trigger=VisionTrigger(object="person"), actions=[legacy]
    )
    assert obstacles(SentryConfigV2(rules=[seen])) == []
    stored = VideoAction.model_validate({"type": "video", "audio_source_id": None})
    assert stored.microphone == "legacy-microphone"


def test_a_video_is_recorded_from_a_satellite_style_camera(hub):
    sentry, _, _, _ = hub
    use(sentry, pir_rule([VideoAction(source_id="camera-b", duration_seconds=1, audio=False)]))
    sentry.arm()
    assert until(lambda: sentry.cameras.camera("camera-b").latest_capture() is not None)
    sentry.observe_event(motion())
    assert until(lambda: events(sentry, "action_finished"), timeout=20)
    [video] = sentry.captures.directory.glob("*.mp4")
    [meta] = sidecars(sentry)
    assert meta["name"] == video.name and meta["kind"] == "video"
    assert meta["source_id"] == "camera-b" and meta["audio_source_id"] is None
    assert (meta["width"], meta["height"]) == (64, 48)
    assert meta["sound"] is False and meta["recorded_seconds"] > 0


# -- files --------------------------------------------------------------------------------


def test_sidecars_follow_their_files_and_leftovers_are_swept(tmp_path):
    captures = Captures(tmp_path)
    name = captures.save_photo(image(5), "front door", {"source_id": "legacy-primary"})
    meta = captures.metadata(name)
    assert meta["schema_version"] == 1 and meta["kind"] == "photo"
    assert (meta["width"], meta["height"], meta["quality"]) == (64, 48, 90)
    legacy = tmp_path / "20260101-000000-000-old-rule.jpg"
    legacy.write_bytes(b"jpeg")
    listing = {c["name"]: c for c in captures.listing()["captures"]}
    assert "evidence" not in listing[legacy.name]
    assert listing[name]["evidence"]["source_id"] == "legacy-primary"
    assert captures.metadata(legacy.name) is None

    stale = tmp_path / "20260101-000001-000-crash.part.mp4"
    fresh = tmp_path / "20260101-000002-000-running.part.mp4"
    lone = tmp_path / "20260101-000003-000-orphan.json"
    for path in (stale, fresh, lone):
        path.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    os.utime(lone, (old, old))
    assert sorted(captures.sweep()) == sorted([stale.name, lone.name])
    assert fresh.exists() and (tmp_path / (Path(name).stem + ".json")).exists()

    captures.delete(name)
    assert not (tmp_path / name).exists()
    assert not (tmp_path / (Path(name).stem + ".json")).exists()


def test_old_captures_take_their_sidecars_with_them(tmp_path):
    captures = Captures(tmp_path)
    with patch("sentry_mode.vision.recording.MAX_CAPTURES", 2):
        names = []
        for _ in range(3):
            names.append(captures.save_photo(image(1), "rule", {"source_id": "x"}))
            time.sleep(0.002)
    assert not (tmp_path / names[0]).exists()
    assert not (tmp_path / (Path(names[0]).stem + ".json")).exists()
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_a_failed_save_leaves_neither_file_nor_sidecar(tmp_path):
    captures = Captures(tmp_path)
    with (
        patch("cv2.imencode", return_value=(False, None)),
        pytest.raises(HardwareError),
    ):
        captures.save_photo(image(1), "rule", {"source_id": "x"})
    assert list(tmp_path.iterdir()) == []


def test_videos_beyond_the_encoding_budget_are_refused(tmp_path):
    captures = Captures(tmp_path, max_recordings=1)
    started = threading.Event()

    def frames():
        started.set()
        return image(1)

    stop = threading.Event()
    worker = threading.Thread(
        target=captures.record_video, args=(frames, 5, "long", stop), daemon=True
    )
    worker.start()
    assert started.wait(5)
    with pytest.raises(HardwareError, match="already being recorded"):
        captures.record_video(frames, 1, "second", threading.Event())
    stop.set()
    worker.join(10)
    name, _ = captures.record_video(frames, 1, "third", threading.Event(), meta={"a": 1})
    assert captures.metadata(name)["recorded_seconds"] == 1.0


# -- previews -----------------------------------------------------------------------------


def test_closing_one_page_keeps_the_camera_for_the_other(hub):
    _, cameras, previews, _ = hub
    camera = cameras.camera("camera-b")
    first = previews.open("camera-b")
    second = previews.open("camera-b")
    assert camera.status()["leases"]["preview"] == 2
    assert until(lambda: camera.status()["capture_running"])
    previews.close(first["session"])
    assert camera.status()["capture_running"]
    assert previews.close(first["session"]) == {"closed": False}
    frames = previews.stream(second["session"])
    assert next(frames).startswith(b"\xff\xd8")
    frames.close()
    listed = {c["source_id"]: c for c in previews.listing()["cameras"]}
    assert listed["camera-b"]["previews"] == 1 and listed["camera-b"]["state"] == "live"
    assert listed["camera-a"]["state"] == "idle"
    assert listed["legacy-primary"]["primary"] and listed["legacy-primary"]["origin"] == "local"
    previews.close(second["session"])
    assert until(lambda: not camera.status()["capture_running"])


def test_a_page_that_stops_renewing_gives_its_camera_back(hub):
    _, cameras, previews, _ = hub
    now = [100.0]
    previews.clock = lambda: now[0]
    session = previews.open("camera-a")
    now[0] += 20
    assert previews.renew(session["session"])["expires_in_seconds"] == 30
    now[0] += 31
    previews.expire()
    with pytest.raises(LookupError):
        previews.renew(session["session"])
    assert cameras.camera("camera-a").status()["leases"]["preview"] == 0


def test_sessions_on_the_node_camera_leave_a_preview_they_did_not_start(hub):
    sentry, _, previews, _ = hub
    video = sentry.video
    session = previews.open("legacy-primary")
    assert video.status()["running"]
    frames = previews.stream(session["session"])
    assert next(frames) == b"primary-jpeg"
    frames.close()
    assert video.viewers == 0
    previews.close(session["session"])
    assert not video.status()["running"]

    video.start()  # the older controls
    session = previews.open("legacy-primary")
    previews.close(session["session"])
    assert video.status()["running"]
    video.stop()


def test_unknown_cameras_and_too_many_pages_are_refused(hub):
    _, _, previews, _ = hub
    with pytest.raises(LookupError):
        previews.open("zero-entrance.pir")
    previews.max_sessions = 1
    previews.open("camera-a")
    with pytest.raises(BlockingIOError):
        previews.open("camera-a")
    with pytest.raises(BlockingIOError, match="no recent picture"):
        previews.snapshot("camera-b")


def test_a_remote_camera_reports_its_own_state_words():
    from sentry_mode.vision.preview import _state

    assert _state({"state": "stale", "capture_running": True}, 9.0) == "stale"
    assert _state({"capture_running": False, "error": "gone"}, None) == "offline"
    assert _state({"capture_running": True}, None) == "starting"
    assert _state({"capture_running": True}, 5.0) == "stale"
    assert _state({"capture_running": False, "error": None}, None) == "idle"


# -- the HTTP side ------------------------------------------------------------------------


@pytest.fixture
def web(tmp_path):
    from http.server import ThreadingHTTPServer

    from sentry_mode.web import NodeControls, make_handler

    controls = NodeControls(
        Settings(
            sentry_state_file=tmp_path / "sentry.json",
            soundboard_file=tmp_path / "soundboard.json",
            soundboard_directory=tmp_path / "soundboard",
            captures_directory=tmp_path / "captures",
            sounds_directory=tmp_path / "sounds",
            camera=CameraConfig(device="rtsp://127.0.0.1:8554/front"),
        )
    )
    other = SyntheticCamera(
        "yard", controls.inference, controls.config.detection, frames=lambda n: image(90)
    )
    controls.cameras.add(other)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controls))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield controls, f"http://127.0.0.1:{server.server_port}"
    finally:
        controls.previews.close_all()
        controls.sentry.disarm()
        controls.video.close()
        controls.cameras.close()
        server.shutdown()
        server.server_close()
        thread.join()
        controls.inference.close()


def call(base, path, body=None):
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    headers = {"X-Sentry-Mode-Control": "1"} if body is not None else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(
        base + path,
        method="POST" if body is not None else "GET",
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        data = response.read()
        if response.headers.get_content_type() == "application/json":
            data = json.loads(data)
        return response.status, data


def test_the_hub_lists_and_previews_each_camera_by_id(web):
    from urllib.request import urlopen

    controls, base = web
    status, listing = call(base, "/api/cameras")
    assert status == 200
    # The node camera is configured with a URL: listing it opens nothing.
    assert [c["source_id"] for c in listing["cameras"]] == ["legacy-primary", "yard"]
    assert listing["cameras"][0]["state"] == "idle"
    status, session = call(base, "/api/cameras/preview/start", {"source_id": "yard"})
    assert status == 200 and session["source_id"] == "yard"
    with urlopen(base + session["stream"], timeout=5) as response:
        assert response.headers.get_content_type() == "multipart/x-mixed-replace"
        assert response.readline() == b"--frame\r\n"
    assert call(base, "/api/cameras/snapshot?source_id=yard")[1][:2] == b"\xff\xd8"
    assert call(base, "/api/cameras/preview/renew", {"session": session["session"]})[0] == 200
    # The older video API is the node camera, whatever a page is looking at.
    assert call(base, "/api/video/status")[1]["running"] is False
    assert call(base, "/api/cameras/preview/stop", {"session": session["session"]})[1] == {
        "closed": True
    }
    assert call(base, "/api/cameras/preview/renew", {"session": session["session"]})[0] == 404
    assert call(base, "/api/cameras/preview/stream?session=" + "x" * 24)[0] == 404
    assert call(base, "/api/cameras/preview/start", {"source_id": "nowhere"})[0] == 404
    assert call(base, "/api/cameras/preview/renew", {"session": "../x"})[0] == 400


def test_the_camera_page_offers_every_camera(web):
    _, base = web
    page = call(base, "/")[1]
    assert b'id="camera-source"' in page and b"/api/cameras/preview/start" in page
    assert b"keepalive:true" in page

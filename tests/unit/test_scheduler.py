"""Several cameras, one model: fair turns, no queues, results kept apart, a stuck model cut off."""

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sentry_mode.config import DetectionConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.sources.manager import DemandTable
from sentry_mode.vision.detection import DetectionWorker
from sentry_mode.vision.scheduler import FramePacket, InferenceScheduler
from sentry_mode.vision.synthetic import SyntheticCamera

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.syspath_prepend(str(HERE))
    import fake_models

    fake_models.Painted.instances = 0
    yield fake_models
    sys.modules.pop("fake_models", None)


def image(label=0, confidence=90, width=32, height=24):
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:, :, 0] = label
    frame[:, :, 1] = confidence
    return frame


def until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return check()


def scheduler(**settings):
    return InferenceScheduler(DetectionConfig(**settings), factory="fake_models:Painted")


def worker(shared, source_id, **settings):
    config = DetectionConfig(enabled=True, **settings)
    client = DetectionWorker(config, shared, source_id)
    seen = []
    client.on_result = lambda detections, captured: seen.append((detections, captured))
    client.configure(True, True)
    return client, seen


def feed(client, frame, stop, every=0.005):
    def run():
        while not stop.is_set():
            client.submit(frame)
            stop.wait(every)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_every_camera_shares_one_model(fakes):
    shared = scheduler()
    cameras = [worker(shared, name) for name in ("a", "b", "c")]
    for client, _ in cameras:
        client.submit(image(1))
    assert until(lambda: all(seen for _, seen in cameras))
    assert fakes.Painted.instances == 1
    shared.close()


def test_a_fast_camera_does_not_starve_a_slow_one():
    shared = scheduler(budget_fps=4)
    fast, fast_seen = worker(shared, "fast", max_fps=10)
    slow, slow_seen = worker(shared, "slow", max_fps=1)
    stop = threading.Event()
    threads = [feed(fast, image(1), stop), feed(slow, image(2), stop)]
    time.sleep(2.6)
    stop.set()
    for thread in threads:
        thread.join()
    shared.close()
    total = len(fast_seen) + len(slow_seen)
    assert total <= 4 * 2.6 + 2  # the budget holds for both together
    assert len(slow_seen) >= 2  # its own rate, not whatever the fast one left over
    assert len(fast_seen) > len(slow_seen)


def test_frames_never_pile_up():
    shared = scheduler(budget_fps=1)
    client, seen = worker(shared, "a")
    for _ in range(500):
        client.submit(image(1))
    assert shared.pending() <= 1
    assert until(lambda: seen)
    assert shared.status()["sources"]["a"]["skipped_frames"] >= 498
    shared.close()


def test_a_frame_already_served_is_not_served_again():
    shared = scheduler()
    served = []
    shared.demand(
        "a",
        "owner",
        fps=10,
        confidence=0.5,
        epoch=1,
        on_result=served.append,
        on_error=lambda message: None,
    )
    packet = FramePacket("a", 1, 7, time.monotonic(), image(1))
    assert shared.offer(packet)
    assert until(lambda: served)
    assert not shared.offer(packet)
    assert not shared.offer(FramePacket("a", 1, 6, time.monotonic(), image(1)))
    assert not shared.offer(FramePacket("a", 0, 99, time.monotonic(), image(1)))  # old epoch
    shared.close()


def test_results_go_only_to_the_camera_that_sent_the_frame():
    shared = scheduler()
    person, person_seen = worker(shared, "a")
    dog, dog_seen = worker(shared, "b")
    person.submit(image(1))
    assert until(lambda: person_seen)
    time.sleep(0.2)
    assert dog_seen == [] and dog.status()["objects"] == []
    assert [d["label"] for d in person.status()["objects"]] == ["person"]
    dog.submit(image(2))
    assert until(lambda: dog_seen)
    assert [d.label for d in dog_seen[0][0]] == ["dog"]
    shared.close()


def test_a_result_from_before_a_restart_is_dropped(fakes):
    release, started = threading.Event(), threading.Event()
    original = fakes.Painted.detect

    def slow(self, frame):
        started.set()
        release.wait(2)
        return original(self, frame)

    fakes.Painted.detect = slow
    try:
        shared = scheduler()
        client, seen = worker(shared, "a")
        other, _ = worker(shared, "b")  # keeps the worker alive across the restart
        client.submit(image(1))
        assert started.wait(2)
        client.pause()
        client.start()
        release.set()
        time.sleep(0.3)
        assert seen == [] and client.status()["objects"] == []
        shared.close()
    finally:
        fakes.Painted.detect = original


def test_the_model_prefilters_for_the_least_demanding_camera():
    shared = scheduler()
    strict, strict_seen = worker(shared, "strict", confidence=0.8)
    lenient, lenient_seen = worker(shared, "lenient", confidence=0.3)
    strict.submit(image(1, confidence=50))
    lenient.submit(image(1, confidence=50))
    assert until(lambda: strict_seen and lenient_seen)
    assert strict_seen[0][0] == []  # found at 0.5, below this camera's own threshold
    assert [d.label for d in lenient_seen[0][0]] == ["person"]
    shared.close()


def test_a_boost_ends_and_never_silences_the_others():
    shared = scheduler(budget_fps=10)
    boosted, _ = worker(shared, "pir-camera", max_fps=2)
    other, other_seen = worker(shared, "other", max_fps=2)
    boosted.boost(3, 0.5)
    assert shared.status("pir-camera")["requested_fps"] == 6
    assert shared.status("pir-camera")["boosted"]
    stop = threading.Event()
    threads = [feed(boosted, image(1), stop), feed(other, image(2), stop)]
    time.sleep(1.2)
    stop.set()
    for thread in threads:
        thread.join()
    assert len(other_seen) >= 2
    assert shared.status("pir-camera")["requested_fps"] == 2
    boosted.boost(100, 3600)
    assert shared.status("pir-camera")["requested_fps"] == 10  # capped, and ends within 30 s
    shared.close()


def test_the_worker_stops_when_nobody_wants_inference():
    shared = scheduler()
    client, _ = worker(shared, "a")
    thread = shared.thread
    assert thread.is_alive()
    client.pause()
    assert not thread.is_alive()
    assert shared.status()["loaded"]  # kept for the next demand


def test_an_inference_failure_reaches_every_camera(fakes):
    original = fakes.Painted.detect
    fakes.Painted.detect = lambda self, frame: (_ for _ in ()).throw(RuntimeError("broken"))
    try:
        shared = scheduler()
        first, _ = worker(shared, "a")
        second, _ = worker(shared, "b")
        first.submit(image(1))
        assert until(lambda: not second.enabled)
        assert first.error == second.error == "broken"
        assert not first.status()["enabled"]
    finally:
        fakes.Painted.detect = original


# -- a model in its own process ----------------------------------------------------------


def test_a_model_that_stops_answering_is_killed_and_replaced():
    shared = InferenceScheduler(
        DetectionConfig(isolation="process", inference_timeout_seconds=1),
        factory="fake_models:Stuck",
    )
    first, first_seen = worker(shared, "a")
    second, _ = worker(shared, "b")
    child = shared._model.process
    first.submit(image(1))
    assert until(lambda: first_seen)
    assert [d.label for d in first_seen[0][0]] == ["person"]
    first.submit(image(1))
    assert until(lambda: not first.enabled and not second.enabled, timeout=10)
    assert "did not answer within 1 second" in first.error
    assert until(lambda: not child.is_alive())
    assert not shared.status()["loaded"]
    first.configure(True, True)  # a fresh child, which answers its first request again
    assert shared._model.process is not child
    first.submit(image(1))
    assert until(lambda: len(first_seen) == 2, timeout=10)
    shared.close()
    assert not shared.status()["loaded"]


def test_a_model_that_cannot_load_in_its_process_says_why():
    shared = InferenceScheduler(DetectionConfig(isolation="process"), factory="fake_models:Missing")
    with pytest.raises(HardwareError, match="missing"):
        shared.load()
    client = DetectionWorker(DetectionConfig(enabled=True), shared, "a")
    with pytest.raises(HardwareError):
        client.configure(True, True)


# -- leases ------------------------------------------------------------------------------


def test_a_lease_is_given_back_once_and_only_by_its_owner():
    changes = []
    table = DemandTable("a", on_change=lambda: changes.append(table.summary()))
    browser = table.hold("preview", "browser")
    recording = table.hold("recording", "rule:x")
    assert table.count("preview") == 1
    assert browser.release() and not browser.release()
    assert table.count("preview") == 0 and table.count("recording") == 1
    assert len(changes) == 3  # two holds and one release; the second release changed nothing
    with recording:
        pass
    assert table.summary() == {"preview": 0, "monitoring": 0, "recording": 0}
    with pytest.raises(ValueError):
        table.hold("streaming", "someone")


def test_the_old_recording_counter_never_goes_below_zero():
    import threading as t

    from sentry_mode.config import CameraConfig
    from sentry_mode.vision.stream import VideoStream

    stream = VideoStream(CameraConfig(), t.Lock())
    stream.add_recording(-1)
    assert stream.recordings == 0
    stream.add_recording(1)
    held = stream.hold_recording("rule:x")
    assert stream.recordings == 2
    stream.add_recording(-1)
    stream.add_recording(-1)  # cannot take away the rule's recording
    assert stream.recordings == 1
    held.release()
    assert stream.recordings == 0
    with pytest.raises(ValueError):
        stream.hold("preview", "browser")


def test_a_synthetic_camera_runs_while_anyone_holds_it():
    shared = scheduler()
    camera = SyntheticCamera("synthetic-a", shared, DetectionConfig(), frames=lambda n: image(1))
    watching = camera.hold("monitoring", "sentry", fps=5, confidence=0.5, detect=True)
    recording = camera.hold("recording", "rule:x")
    assert until(lambda: camera.status()["detection"]["objects"])
    watching.release()
    status = camera.status()
    assert status["capture_running"]  # the recording still needs frames
    assert not status["detection"]["enabled"]
    assert shared.status()["sources"]["synthetic-a"]["requested_fps"] == 0
    recording.release()
    assert not camera.status()["capture_running"]
    assert camera.latest_frame() is None
    shared.close()

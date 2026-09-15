import threading
import time
from unittest.mock import patch

import numpy as np
import pytest

from sentry_mode.config import DetectionConfig
from sentry_mode.core.errors import HardwareError
from sentry_mode.vision.detection import Detection, DetectionWorker, ObjectDetector, annotate


def test_model_missing_is_explicit(tmp_path):
    with pytest.raises(HardwareError, match="setup_detection"):
        ObjectDetector(DetectionConfig(model=tmp_path / "missing.onnx"))


def test_decode_letterbox_confidence_and_classwise_suppression(tmp_path):
    model = tmp_path / "model.onnx"
    model.touch()
    with patch("cv2.dnn.readNetFromONNX") as read:
        detector = ObjectDetector(DetectionConfig(model=model))
        raw = np.zeros((1, 3549, 85), dtype=np.float32)
        # First grid point, center (208,104), size (208,104) in 416px model space.
        for index, cls in [(0, 0), (1, 0), (2, 16)]:
            raw[0, index, :4] = [26 - index, 13, np.log(26), np.log(13)]
            raw[0, index, 4] = 0.9
            raw[0, index, 5 + cls] = 0.9
        raw[0, 3, 4:] = 0.1
        read.return_value.forward.return_value = raw
        result = detector.detect(np.zeros((200, 400, 3), np.uint8))
    assert {r.label for r in result} == {"person", "dog"}
    assert len(result) == 2  # Duplicate person is suppressed, overlapping dog is retained.
    assert result[0].box == pytest.approx((0.25, 0.25, 0.75, 0.75))
    assert result[0].confidence == pytest.approx(0.81)


def test_disabling_inflight_detection_discards_result_and_frees_worker():
    started, release = threading.Event(), threading.Event()
    worker = DetectionWorker(DetectionConfig())

    def detect(frame):
        started.set()
        assert release.wait(2)
        return [Detection("person", 0.9, (0.1, 0.1, 0.9, 0.9))]

    with patch("sentry_mode.vision.detection.ObjectDetector") as factory:
        factory.return_value.detect.side_effect = detect
        worker.configure(True, True)
        worker.submit(np.zeros((60, 80, 3), np.uint8))
        assert started.wait(2)
        stopping = threading.Thread(target=worker.configure, args=(False, True))
        stopping.start()
        assert worker.stopped.wait(1)
        release.set()
        stopping.join(2)
    assert not stopping.is_alive()
    assert not worker.thread.is_alive()
    assert worker.status()["objects"] == []
    assert not worker.status()["enabled"]


def test_inference_failure_does_not_crash_video_and_stale_boxes_expire():
    worker = DetectionWorker(DetectionConfig())
    with patch("sentry_mode.vision.detection.ObjectDetector") as factory:
        factory.return_value.detect.side_effect = RuntimeError("inference failed")
        worker.configure(True, True)
        worker.submit(np.zeros((60, 80, 3), np.uint8))
        worker.thread.join(2)
    assert worker.status()["error"] == "inference failed"
    assert not worker.status()["enabled"]
    worker.enabled = True
    worker.result = [Detection("person", 0.9, (0.1, 0.1, 0.9, 0.9))]
    worker.updated = time.monotonic() - 2
    assert worker.status()["objects"] == []
    worker.pause()


def test_annotation_does_not_change_inference_input():
    frame = np.zeros((120, 200, 3), np.uint8)
    output = annotate(frame, [Detection("person", 0.9, (0.1, 0.1, 0.9, 0.9))])
    assert output.any()
    assert not frame.any()

"""Detectors that stand in for the model, importable by a spawned inference process."""

import threading

from sentry_mode.vision.detection import Detection

LABELS = {0: [], 1: ["person"], 2: ["dog"], 3: ["person", "dog"]}


class Painted:
    """Finds whatever the frame's first pixel says is there, at the confidence its green says."""

    instances = 0
    lock = threading.Lock()

    def __init__(self, config):
        self.config = config
        with Painted.lock:
            Painted.instances += 1

    def detect(self, frame):
        found = []
        confidence = int(frame[0, 0, 1]) / 100 or 0.9
        for label in LABELS[int(frame[0, 0, 0]) % 4]:
            if confidence >= self.config.confidence:
                found.append(Detection(label, confidence, (0.1, 0.1, 0.5, 0.5)))
        return found


class Stuck(Painted):
    """Never answers the second request."""

    calls = 0

    def detect(self, frame):
        Stuck.calls += 1
        if Stuck.calls > 1:
            threading.Event().wait()
        return super().detect(frame)


class Missing:
    def __init__(self, config):
        raise RuntimeError("Object model is missing.")

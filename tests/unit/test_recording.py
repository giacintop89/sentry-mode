import threading
from unittest.mock import patch

import numpy as np
import pytest

from sentry_node.core.errors import HardwareError
from sentry_node.vision import recording
from sentry_node.vision.recording import Captures


class FakeEncoder:
    def __init__(self, args, **kwargs):
        self.args, self.frames, self.returncode = args, 0, None
        self.stdin = self

    def write(self, data):
        self.frames += 1

    def communicate(self, timeout=None):
        with open(self.args[-1], "wb") as output:
            output.write(b"mp4")
        self.returncode = 0
        return b"", b""

    def poll(self):
        return self.returncode


def test_video_lasts_the_requested_time_at_a_fixed_even_size(tmp_path):
    captures = Captures(tmp_path)
    frame = np.zeros((360, 640, 3), np.uint8)
    encoders = []

    def start(args, **kwargs):
        encoders.append(FakeEncoder(args))
        return encoders[0]

    with (
        patch("sentry_node.vision.recording.subprocess.Popen", side_effect=start),
        patch.object(threading.Event, "wait", return_value=False),
    ):
        name, audio_error = captures.record_video(
            lambda: frame, 2, "Front door!", threading.Event(), (1921, 1081)
        )
    assert audio_error is None
    args = encoders[0].args
    assert encoders[0].frames == 2 * recording.VIDEO_FPS
    assert args[args.index("-s") + 1] == "1280x720" and "libx264" in args
    assert name.endswith("-front-door.mp4") and (tmp_path / name).read_bytes() == b"mp4"
    assert not list(tmp_path.glob("*.part*"))


def test_stopping_early_keeps_the_recording_and_missing_camera_fails(tmp_path):
    captures = Captures(tmp_path)
    stop = threading.Event()
    stop.set()
    with patch("sentry_node.vision.recording.subprocess.Popen", side_effect=FakeEncoder):
        name, _ = captures.record_video(lambda: np.zeros((72, 128, 3), np.uint8), 30, "r", stop)
        assert captures.path(name).is_file()
        with pytest.raises(HardwareError, match="No camera frame"):
            captures.record_video(lambda: None, 5, "r", stop)


def test_captures_are_bounded_and_names_cannot_escape_the_folder(tmp_path):
    captures = Captures(tmp_path / "captures")
    frame = np.zeros((8, 8, 3), np.uint8)
    with patch.object(recording, "MAX_CAPTURES", 3):
        names = [captures.save_photo(frame, f"rule {i}") for i in range(5)]
    kept = [c["name"] for c in captures.listing()["captures"]]
    assert kept == names[:1:-1]
    (tmp_path / "secret.jpg").write_bytes(b"x")
    for name in ("../secret.jpg", "secret.jpg", names[-1] + "/x"):
        with pytest.raises(LookupError):
            captures.path(name)


def test_audio_recordings_and_messages_are_saved_and_listed(tmp_path):
    captures = Captures(tmp_path)
    silence = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
    name = captures.record_audio(silence, 1, "Front door!", threading.Event())
    assert name.endswith("-front-door.m4a")
    message = captures.save_message(captures.path(name).read_bytes())
    assert message.endswith("-phone-message.m4a")
    kinds = {c["name"]: c["kind"] for c in captures.listing()["captures"]}
    assert kinds == {name: "audio", message: "audio"}
    with pytest.raises(ValueError, match="empty"):
        captures.save_message(b"")
    with pytest.raises(ValueError, match="Could not save"):
        captures.save_message(b"not audio at all")


def test_video_keeps_recording_when_its_sound_fails(tmp_path):
    captures = Captures(tmp_path)
    frame = np.zeros((72, 128, 3), np.uint8)
    stop = threading.Event()
    name, audio_error = captures.record_video(
        lambda: frame, 1, "quiet", stop, microphone=["-f", "lavfi", "-i", "no-such-input"]
    )
    assert audio_error and captures.path(name).stat().st_size
    assert not list(tmp_path.glob("*.part*"))
